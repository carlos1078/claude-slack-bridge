# Plan: `review_plan` MCP Tool

A new MCP tool that lets Claude request a human review of a plan (markdown)
via a short-lived ngrok-exposed web UI with GitHub-PR-style inline comments.

## Goal

When the user prompts something like *"create a plan and use review-plan and
send it to Slack"*, Claude produces a plan in markdown and calls the new
`review_plan` MCP tool. The tool:

1. Spins up a local HTTP server + fresh ngrok tunnel.
2. Posts the ngrok URL to Slack.
3. Blocks until the user clicks **Approve** or **Request Changes** in the
   browser UI.
4. Returns the outcome directly to Claude as the MCP tool result.
5. Tears down the tunnel + server.

Each round is fully fresh: new ngrok URL, no state carried from prior rounds.

## Tool contract

```python
review_plan(plan_markdown: str) -> {
    "status": "approved" | "changes_requested",
    "comments": [{"line": int, "text": str}],   # empty when approved
}
```

- When `status == "approved"`, Claude treats the plan as final and moves on.
- When `status == "changes_requested"`, Claude receives the structured list
  of per-line comments and revises the plan, then calls `review_plan` again
  with the new markdown (new ngrok URL posted to Slack).

**Feedback format decision:** return structured `comments` list (option a).
Cleaner for Claude to reason over than a pre-formatted markdown blob.

## Per-call lifecycle (lives inside the session process)

1. Load `REVIEW_AUTH_KEY` and `NGROK_AUTHTOKEN` from `.env`.
2. Start aiohttp server on an ephemeral port inside the Docker container.
3. Spawn `ngrok http <port>` as a subprocess (binary installed in the image).
4. Poll `http://127.0.0.1:4040/api/tunnels` until the public URL appears.
5. Post `https://<tunnel>.ngrok-free.app/review?key=<REVIEW_AUTH_KEY>` to
   the project's `SLACK_CHANNEL` (reuse existing Slack bridge).
6. `await asyncio.Event` set by `POST /submit`.
7. Terminate the ngrok subprocess + stop the HTTP server.
8. Return result to Claude.

If the user closes the page without clicking either button, the call times
out using the same `TIMEOUT_LIMIT_MINUTES` env var already used by
`ask_on_slack` (defaults to 5 minutes).

## Server endpoints

| Method | Path     | Behavior |
|--------|----------|----------|
| GET    | `/review?key=...` | Returns 403 if key mismatch. Else returns HTML with rendered markdown + inline comment UI. |
| POST   | `/submit` | Body `{action: "approved" \| "changes_requested", comments: [...]}`. Sets the waiter event, responds with a "you can close this tab" page. |

## UI (`review.html`, vanilla JS, no build step)

- Server-side markdown → HTML via `markdown-it-py`. One line per
  `<div data-line="N">` so comments can anchor cleanly.
- Click a line → inline textarea appears below it; submit adds the comment
  to a pending-comments panel.
- Comments are kept client-side until the user presses one of the action
  buttons — submission is a single POST.
- Two action buttons in a sticky top bar:
  - **Approve** (green) — ends the loop. Sends `{action: "approved", comments: []}`.
  - **Request Changes** (red) — disabled unless ≥1 comment exists. Sends
    `{action: "changes_requested", comments: [...]}`.
- Auth: every request to the server must include the `key` query param.
  Served only over ngrok's HTTPS.

## Security

- `REVIEW_AUTH_KEY` is a long random string in `.env`, shared between the
  server and the URL posted to Slack.
- ngrok URL is randomly named and short-lived (torn down after one review).
- The key protects against anyone sniffing the URL out of Slack history
  trying to reuse it; combined with the fresh-tunnel-per-call rule, reuse
  windows are minutes, not forever.

## Files

### New
- [src/review_server.py](../src/review_server.py) — aiohttp app + ngrok
  lifecycle + asyncio event waiter.
- [src/templates/review.html](../src/templates/review.html) — UI template.

### Edited
- [src/mcp_server.py](../src/mcp_server.py) — register `review_plan`
  alongside existing `ask_on_slack`.
- [.env.example](../.env.example) — document new vars.
- [requirements.txt](../requirements.txt) — add `markdown-it-py`, `aiohttp`.
- [Dockerfile](../Dockerfile) — install `ngrok` binary (apt repo or direct
  download with checksum verification), configure authtoken at container
  start from `NGROK_AUTHTOKEN`.
- [README.md](../README.md) — document feature + one-time setup.
- [docker-compose.yml](../docker-compose.yml) — likely no change (outbound
  traffic already works for Slack); verify during implementation.

## Opt-in design

The feature is fully optional — users who don't want ngrok never have the
binary in their image and never see the tool. Both the build and runtime
are driven by a single source of truth: **presence of `NGROK_AUTHTOKEN` in
`.env`.**

| User action | Result |
|---|---|
| `NGROK_AUTHTOKEN` unset, `docker compose up -d --build` | Lean image, no ngrok binary. `review_plan` not registered. `ask_on_slack` unchanged. |
| `NGROK_AUTHTOKEN=…` set in `.env`, `docker compose up -d --build` | ngrok installed in image. `review_plan` registered. Feature live. |
| Token removed later + rebuild | Back to lean image, feature gone. |

### Mechanism

**`docker-compose.yml`** passes a build arg derived from the env var:

```yaml
services:
  daemon:
    build:
      context: .
      args:
        INSTALL_NGROK: ${NGROK_AUTHTOKEN:+1}
```

`${VAR:+1}` = `"1"` when non-empty, empty string otherwise.

**`Dockerfile`** conditionally installs:

```dockerfile
ARG INSTALL_NGROK
RUN if [ "$INSTALL_NGROK" = "1" ]; then \
      # download + verify ngrok binary, install to /usr/local/bin \
    ; fi
```

**`mcp_server.py`** gates tool registration at runtime:

```python
if os.getenv("NGROK_AUTHTOKEN"):
    mcp.tool()(self.review_plan)
```

If `NGROK_AUTHTOKEN` is set but `REVIEW_AUTH_KEY` is missing, the tool
surfaces a clear error at call time (not at startup).

## One-time user setup

Add to `.env`:

```env
NGROK_AUTHTOKEN=<from ngrok dashboard>
REVIEW_AUTH_KEY=<long random string, e.g. `openssl rand -hex 32`>
```

Rebuild the daemon container:

```bash
docker compose up -d --build
```

## Open questions / risks

- **ngrok inside Docker**: binary baked into the image at build time.
  Container has outbound network already (Slack works), so tunnels will
  connect fine. ngrok's authtoken needs to be set once on first container
  run via `ngrok config add-authtoken $NGROK_AUTHTOKEN` (done in
  entrypoint or lazily before first use).
- **Free ngrok rate limits**: a busy reviewer could hit ngrok free-tier
  limits (tunnel count, bandwidth). Acceptable for personal use; document
  as a caveat.
- **Line-number anchoring after markdown render**: ensure the renderer
  preserves a 1:1 mapping between source lines and rendered blocks so
  comments anchor to the right place. `markdown-it-py` supports source
  maps via the `sourceMap` env option — verify.
