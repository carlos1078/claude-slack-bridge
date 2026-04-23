"""
review_server.py — Human-in-the-loop plan review via an ngrok-tunneled
aiohttp UI.

One review round == one ``ReviewSession`` instance:
  1. Start an aiohttp app on a free local port.
  2. Spawn an ``ngrok http <port>`` subprocess and resolve its public URL.
  3. Post the URL to Slack via the caller-provided ``post_message``.
  4. Block on an ``asyncio.Event`` until the user submits via POST /submit
     (or the configured timeout elapses).
  5. Tear everything down — the next call starts from scratch with a new URL.
"""

import asyncio
import json
import logging
import os
import socket
import subprocess
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

NGROK_API = "http://127.0.0.1:4040/api/tunnels"
NGROK_STARTUP_TIMEOUT_SECONDS = 30.0
NGROK_POLL_INTERVAL_SECONDS = 0.5

TEMPLATE_PATH = Path(__file__).parent / "templates" / "review.html"

PostMessageFn = Callable[[str, str | None], Coroutine[Any, Any, str]]


def _pick_free_port() -> int:
    """Bind to port 0 and return whatever the OS hands back."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _render_plan_html(plan_markdown: str) -> str:
    """
    Render the plan into top-level HTML blocks, each wrapped in a
    ``<div class="block" data-line="N">`` anchor so the browser can attach
    inline comments to source lines. ``N`` is 1-based, matching how humans
    read line numbers and how Claude reasons about them.
    """
    md = MarkdownIt("commonmark", {"html": False, "breaks": False}).enable("table")
    env: dict[str, Any] = {}
    tokens = md.parse(plan_markdown, env)

    chunks: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.nesting == 1:
            depth = 1
            j = i + 1
            while j < len(tokens) and depth > 0:
                if tokens[j].nesting == 1:
                    depth += 1
                elif tokens[j].nesting == -1:
                    depth -= 1
                j += 1
            group = tokens[i:j]
            line = (tok.map[0] + 1) if tok.map else 0
            html = md.renderer.render(group, md.options, env)
            chunks.append(f'<div class="block" data-line="{line}">{html}</div>')
            i = j
        elif tok.nesting == 0:
            line = (tok.map[0] + 1) if tok.map else 0
            html = md.renderer.render([tok], md.options, env)
            chunks.append(f'<div class="block" data-line="{line}">{html}</div>')
            i += 1
        else:
            i += 1
    return "\n".join(chunks)


class ReviewSession:
    """
    One-shot human review of a plan. Instances are not reusable — create a
    new ``ReviewSession`` per call to ``review_plan``.

    Args:
        plan_markdown:   The plan text to review.
        auth_key:        Long random shared secret included in the URL and
                         required on every request to the server.
        timeout_seconds: Maximum time to wait for a submission.
    """

    def __init__(
        self,
        plan_markdown: str,
        auth_key: str,
        timeout_seconds: float,
    ) -> None:
        self._plan = plan_markdown
        self._auth_key = auth_key
        self._timeout = timeout_seconds

        self._event = asyncio.Event()
        self._result: dict[str, Any] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ngrok: subprocess.Popen[bytes] | None = None
        self._port: int = 0

    async def _handle_review(self, request: web.Request) -> web.Response:
        if request.query.get("key") != self._auth_key:
            return web.Response(status=403, text="Forbidden")
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        # AUTH_KEY is a hex string so it's safe inside the <script> tag.
        # Replace AUTH_KEY first so a plan that literally contains the
        # RENDERED sentinel cannot leak the key.
        html = template.replace("%%AUTH_KEY%%", self._auth_key).replace(
            "%%RENDERED%%", _render_plan_html(self._plan)
        )
        return web.Response(text=html, content_type="text/html")

    async def _handle_submit(self, request: web.Request) -> web.Response:
        if request.query.get("key") != self._auth_key:
            return web.Response(status=403, text="Forbidden")
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="Bad JSON")

        action = payload.get("action")
        raw_comments = payload.get("comments") or []
        if action not in ("approved", "changes_requested"):
            return web.Response(status=400, text="Bad action")
        if action == "changes_requested" and not raw_comments:
            return web.Response(status=400, text="Comments required")

        cleaned: list[dict[str, Any]] = []
        for c in raw_comments:
            try:
                cleaned.append({"line": int(c["line"]), "text": str(c["text"])})
            except (KeyError, ValueError, TypeError):
                return web.Response(status=400, text="Bad comment entry")

        self._result = {
            "status": action,
            "comments": [] if action == "approved" else cleaned,
        }
        self._event.set()
        return web.Response(text="Submitted. You can close this tab.")

    async def _start_http(self) -> None:
        app = web.Application()
        app.router.add_get("/review", self._handle_review)
        app.router.add_post("/submit", self._handle_submit)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._port = _pick_free_port()
        self._site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await self._site.start()
        logger.info("Review HTTP server listening on 127.0.0.1:%d", self._port)

    async def _ensure_ngrok_authtoken(self) -> None:
        token = os.getenv("NGROK_AUTHTOKEN")
        if not token:
            raise RuntimeError("NGROK_AUTHTOKEN is not set; cannot start ngrok.")
        proc = await asyncio.create_subprocess_exec(
            "ngrok",
            "config",
            "add-authtoken",
            token,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def _start_ngrok(self) -> str:
        await self._ensure_ngrok_authtoken()
        self._ngrok = subprocess.Popen(
            ["ngrok", "http", f"127.0.0.1:{self._port}", "--log=stdout"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + NGROK_STARTUP_TIMEOUT_SECONDS
        async with aiohttp.ClientSession() as session:
            while loop.time() < deadline:
                try:
                    async with session.get(NGROK_API) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for tun in data.get("tunnels") or []:
                                url = tun.get("public_url", "")
                                if url.startswith("https://"):
                                    return url
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(NGROK_POLL_INTERVAL_SECONDS)

        raise RuntimeError(
            f"ngrok tunnel did not become ready within "
            f"{NGROK_STARTUP_TIMEOUT_SECONDS:.0f}s."
        )

    async def _teardown(self) -> None:
        if self._ngrok is not None:
            try:
                self._ngrok.terminate()
                try:
                    self._ngrok.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._ngrok.kill()
            except Exception as exc:
                logger.warning("ngrok teardown error: %s", exc)
            self._ngrok = None
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception as exc:
                logger.warning("site teardown error: %s", exc)
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:
                logger.warning("runner cleanup error: %s", exc)
            self._runner = None
        logger.info("Review server + ngrok torn down.")

    async def run(self, post_message: PostMessageFn) -> dict[str, Any]:
        """
        Run a full review round.

        Args:
            post_message: Async ``(text, thread_ts | None) -> thread_ts``. The
                          review URL is posted as a fresh top-level message.

        Returns:
            ``{"status": "approved" | "changes_requested", "comments": [...]}``.

        Raises:
            RuntimeError: On timeout, missing ``NGROK_AUTHTOKEN``, or an
                          ngrok startup failure.
        """
        try:
            await self._start_http()
            public_url = await self._start_ngrok()
            review_url = f"{public_url}/review?key={self._auth_key}"
            await post_message(
                f":memo: *Plan review requested.* {review_url}",
                None,
            )
            logger.info("Posted review URL (tunnel %s).", public_url)

            try:
                await asyncio.wait_for(self._event.wait(), timeout=self._timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"No review submission received within "
                    f"{int(self._timeout // 60)} minutes."
                )

            assert self._result is not None
            return self._result
        finally:
            await self._teardown()
