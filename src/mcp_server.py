"""
mcp_server.py — MCP server module.

Registers MCP tools on a FastMCP instance:
  - ``ask_on_slack``  — always registered, bridges Claude to a human via
    threaded Slack replies.
  - ``review_plan``   — registered only when ``NGROK_AUTHTOKEN`` is present in
    the environment. Spins up a short-lived, ngrok-tunneled web UI that lets
    a human approve the plan or leave per-line comments.

This class does not own the FastMCP instance; it receives it via
``register()`` so the entry point retains full control over the server
lifecycle.
"""

import logging
import os
from typing import Any

from fastmcp import FastMCP

from review_server import PostMessageFn, ReviewSession

logger = logging.getLogger(__name__)


class MCPServer:
    """
    Registers MCP tools that bridge Claude Code to Slack.

    Args:
        broker:          Any object with ``send_and_wait(message: str) -> str``.
                         Used by ``ask_on_slack``.
        post_message:    Async ``(text, thread_ts | None) -> thread_ts``. Used
                         by ``review_plan`` to post the review URL. Required
                         for ``review_plan`` registration; may be ``None`` when
                         only ``ask_on_slack`` is needed.
        timeout_minutes: Timeout used by ``review_plan`` while waiting for a
                         human submission. Shares the ``TIMEOUT_LIMIT_MINUTES``
                         env var used elsewhere.
    """

    def __init__(
        self,
        broker: Any,
        post_message: PostMessageFn | None = None,
        timeout_minutes: int = 5,
    ) -> None:
        self._broker = broker
        self._post_message = post_message
        self._timeout_seconds = float(timeout_minutes) * 60.0

    def register(self, mcp: FastMCP) -> None:
        """
        Register all MCP tools on the provided FastMCP instance.

        Call this once during application startup, before running the server.

        Args:
            mcp: The FastMCP server instance owned by ``session.py``.
        """
        mcp.tool()(self.ask_on_slack)
        logger.info("Registered 'ask_on_slack' tool on MCP server.")

        # Opt-in: review_plan only surfaces when the user has configured ngrok.
        if os.getenv("NGROK_AUTHTOKEN"):
            if self._post_message is None:
                logger.warning(
                    "NGROK_AUTHTOKEN is set but post_message was not provided; "
                    "review_plan will not be registered."
                )
            else:
                mcp.tool()(self.review_plan)
                logger.info("Registered 'review_plan' tool on MCP server.")

    async def ask_on_slack(self, message: str) -> str:
        """
        Post a message to Slack and wait for a human reply.

        Use this tool whenever you need a human decision, clarification, or
        approval that cannot be determined from existing context. The tool
        blocks until a reply is received in the Slack thread (up to 5 minutes).

        Args:
            message: The question or message to send to the Slack channel.

        Returns:
            The text of the human's reply.

        Raises:
            RuntimeError: If no reply is received within 5 minutes.
        """
        logger.info("ask_on_slack called with message: %r", message)
        reply = await self._broker.send_and_wait(message)
        return reply

    async def review_plan(self, plan_markdown: str) -> dict[str, Any]:
        """
        Request a human review of a plan written in markdown.

        Spins up a short-lived web UI (fresh ngrok URL) that renders the plan
        with inline per-line comment boxes and two actions: **Approve** and
        **Request changes**. Posts the URL to the Slack channel configured for
        this session and blocks until the user submits.

        Use this when you have produced a plan and want a structured human
        decision before proceeding. When ``status == "changes_requested"``,
        revise the plan using the ``comments`` and call this tool again with
        the new markdown — each call opens a fresh URL.

        Args:
            plan_markdown: The plan to review, in markdown. Line numbers in
                           the returned comments are 1-based against this text.

        Returns:
            ``{
                "status": "approved" | "changes_requested",
                "comments": [{"line": int, "text": str}, ...],
            }``
            ``comments`` is empty when the plan is approved.

        Raises:
            RuntimeError: If ``REVIEW_AUTH_KEY`` is unset, if the ngrok tunnel
                          fails to come up, or if the user does not submit
                          within the configured timeout.
        """
        auth_key = os.getenv("REVIEW_AUTH_KEY")
        if not auth_key:
            raise RuntimeError(
                "REVIEW_AUTH_KEY is not set. Add a long random string "
                "(e.g. `openssl rand -hex 32`) to .env to use review_plan."
            )
        if self._post_message is None:
            raise RuntimeError("review_plan has no Slack post_message configured.")

        logger.info("review_plan called (%d chars of markdown).", len(plan_markdown))
        session = ReviewSession(
            plan_markdown=plan_markdown,
            auth_key=auth_key,
            timeout_seconds=self._timeout_seconds,
        )
        result = await session.run(self._post_message)
        logger.info("review_plan returning status=%s", result.get("status"))
        return result
