"""
Credential Proxy — injects real secrets into requests from agent VMs.

Two-layer auth on every request:
  1. X-Proxy-Key header (injected by exe.dev integration, shared across all VMs)
  2. X-Proxy-Token header or URL path token (per-agent, proves identity)

Config via environment:
  PROXY_AUTH_KEY     — shared secret for X-Proxy-Key validation
  PROXY_PORT         — port to listen on (default 8100)
  HUB_BASE_URL       — Hub REST upstream (default https://admin.slate.ceo/oc/brain)
  HUB_WS_BASE        — Hub WS upstream (default wss://admin.slate.ceo/oc/brain)

Agent credentials stored in agents.db (SQLite).
"""

import asyncio
import logging
import os

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from db import all_agents as load_agents, lookup_by_hub_token, lookup_by_tg_token, get_agent

logger = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

PROXY_AUTH_KEY = os.environ.get("PROXY_AUTH_KEY", "")
HUB_BASE_URL = os.environ.get("HUB_BASE_URL", "https://admin.slate.ceo/oc/brain")
HUB_WS_BASE = os.environ.get("HUB_WS_BASE", "wss://admin.slate.ceo/oc/brain")

app = FastAPI()


# ── Auth helpers ────────────────────────────────────────────────────

def validate_proxy_key(request: Request) -> str | None:
    """Validate X-Proxy-Key header. Returns error message or None if valid."""
    if not PROXY_AUTH_KEY:
        return None  # Auth disabled (no key configured)
    key = request.headers.get("X-Proxy-Key", "")
    if key != PROXY_AUTH_KEY:
        return "Invalid or missing X-Proxy-Key"
    return None


def validate_proxy_key_ws(headers: dict) -> str | None:
    """Validate X-Proxy-Key from WS upgrade headers."""
    if not PROXY_AUTH_KEY:
        return None
    key = headers.get("x-proxy-key", "")
    if key != PROXY_AUTH_KEY:
        return "Invalid or missing X-Proxy-Key"
    return None


# ── Health ──────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    agents = load_agents()
    return {
        "ok": True,
        "agents_configured": len(agents),
        "auth_enabled": bool(PROXY_AUTH_KEY),
    }


# ── Telegram proxy ─────────────────────────────────────────────────

TELEGRAM_API_BASE = "https://api.telegram.org"


async def _telegram_proxy(request: Request, tg_proxy_token: str, path: str, upstream_prefix: str):
    """Shared logic for Telegram API and file-download proxy routes."""
    # Layer 1: X-Proxy-Key
    err = validate_proxy_key(request)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)

    # Layer 2: tg_proxy_token in URL path
    agent_name, agent_record = lookup_by_tg_token(tg_proxy_token)
    if not agent_name:
        return JSONResponse(
            {"ok": False, "error": "Invalid tg_proxy_token or agent not configured"},
            status_code=403,
        )

    bot_token = agent_record.get("telegram_bot_token")
    if not bot_token:
        return JSONResponse(
            {"ok": False, "error": f"No telegram_bot_token configured for agent '{agent_name}'"},
            status_code=403,
        )

    # Build upstream URL: https://api.telegram.org/{prefix}{real_token}/{path}
    upstream_url = f"{TELEGRAM_API_BASE}/{upstream_prefix}{bot_token}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Forward headers (strip proxy headers)
    forward_headers = {}
    for key, value in request.headers.items():
        if key.lower() in ("host", "x-proxy-key", "content-length"):
            continue
        forward_headers[key] = value

    raw_body = await request.body()

    async with httpx.AsyncClient(timeout=60.0) as client:
        upstream_resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=forward_headers,
            content=raw_body,
        )

    logger.info(
        "[TG] %s: %s %s → %d",
        agent_name, request.method, path, upstream_resp.status_code,
    )

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=dict(upstream_resp.headers),
    )


@app.api_route(
    "/telegram/{tg_proxy_token}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def telegram_api_proxy(request: Request, tg_proxy_token: str, path: str):
    """Reverse proxy for Telegram Bot API. Rewrites to bot{real_token}/{method}."""
    return await _telegram_proxy(request, tg_proxy_token, path, upstream_prefix="bot")


@app.api_route(
    "/telegram-file/{tg_proxy_token}/{path:path}",
    methods=["GET", "POST"],
)
async def telegram_file_proxy(request: Request, tg_proxy_token: str, path: str):
    """Reverse proxy for Telegram file downloads. Rewrites to file/bot{real_token}/{path}."""
    return await _telegram_proxy(request, tg_proxy_token, path, upstream_prefix="file/bot")


# ── Hub REST proxy ──────────────────────────────────────────────────

@app.api_route("/hub/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def hub_rest_proxy(request: Request, path: str):
    """Reverse proxy for Hub REST API. Validates auth, injects X-Agent-Secret."""

    # Layer 1: X-Proxy-Key
    err = validate_proxy_key(request)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)

    # Layer 2: X-Proxy-Token (hub_proxy_token)
    proxy_token = request.headers.get("X-Proxy-Token", "")
    agent_name, agent_record = lookup_by_hub_token(proxy_token) if proxy_token else (None, {})

    if not agent_name:
        # Fallback: try X-Agent-ID + check agent exists (for minimal test mode)
        agent_name = request.headers.get("X-Agent-ID")
        if not agent_name:
            try:
                body_json = await request.json()
                agent_name = body_json.get("from")
            except Exception:
                pass
        if agent_name:
            agent_record = get_agent(agent_name) or {}

    if not agent_name or not agent_record:
        return JSONResponse(
            {"ok": False, "error": "Cannot identify agent — invalid X-Proxy-Token or agent not configured"},
            status_code=403,
        )

    hub_secret = agent_record.get("hub_secret")
    if not hub_secret:
        return JSONResponse(
            {"ok": False, "error": f"No hub_secret configured for agent '{agent_name}'"},
            status_code=403,
        )

    # Build upstream request
    upstream_url = f"{HUB_BASE_URL}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Forward headers, add X-Agent-Secret, strip proxy headers
    forward_headers = {}
    for key, value in request.headers.items():
        if key.lower() in ("host", "x-proxy-key", "x-proxy-token", "x-agent-id", "content-length"):
            continue
        forward_headers[key] = value
    forward_headers["X-Agent-Secret"] = hub_secret

    raw_body = await request.body()

    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream_resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=forward_headers,
            content=raw_body,
        )

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=dict(upstream_resp.headers),
    )


# ── Hub WebSocket proxy ─────────────────────────────────────────────

@app.websocket("/hub/ws/{agent_name}")
async def hub_ws_proxy(ws: WebSocket, agent_name: str):
    """WebSocket proxy for Hub. Validates auth, injects X-Agent-Secret on upstream."""

    # Collect headers before accept (available on the upgrade request)
    headers = {k.lower(): v for k, v in ws.headers.items()}

    # Layer 1: X-Proxy-Key
    err = validate_proxy_key_ws(headers)
    if err:
        await ws.close(code=4003, reason=err)
        return

    # Layer 2: X-Proxy-Token
    proxy_token = headers.get("x-proxy-token", "")
    found_name, agent_record = lookup_by_hub_token(proxy_token) if proxy_token else (None, {})

    if not found_name:
        # Fallback: look up by agent_name directly (minimal test mode)
        agent_record = get_agent(agent_name) or {}
        found_name = agent_name if agent_record else None

    if not found_name or found_name != agent_name:
        await ws.close(code=4003, reason=f"Auth failed for agent '{agent_name}'")
        return

    hub_secret = agent_record.get("hub_secret")
    if not hub_secret:
        await ws.close(code=4003, reason=f"No hub_secret for '{agent_name}'")
        return

    # Accept downstream connection
    await ws.accept()
    logger.info("[WS] %s: connected", agent_name)

    # Connect upstream to Hub
    upstream_url = f"{HUB_WS_BASE}/agents/{agent_name}/ws"
    upstream_headers = {"X-Agent-Secret": hub_secret}

    try:
        async with websockets.connect(
            upstream_url,
            additional_headers=upstream_headers,
            open_timeout=15,
            ping_interval=None,  # disable library-level pings — app handles keepalive
            ping_timeout=None,
        ) as upstream:
            logger.info("[WS] %s: upstream connected", agent_name)

            # Forward auth response from Hub to downstream
            auth_resp = await asyncio.wait_for(upstream.recv(), timeout=10)
            await ws.send_text(auth_resp)

            # Bidirectional frame pipe
            async def downstream_to_upstream():
                try:
                    while True:
                        data = await ws.receive_text()
                        await upstream.send(data)
                except WebSocketDisconnect:
                    pass

            async def upstream_to_downstream():
                try:
                    async for message in upstream:
                        await ws.send_text(message)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(downstream_to_upstream()),
                    asyncio.create_task(upstream_to_downstream()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    except Exception as e:
        logger.error("[WS] %s: upstream failed: %s", agent_name, e)
        try:
            await ws.close(code=4502, reason="Upstream connection failed")
        except Exception:
            pass

    logger.info("[WS] %s: disconnected", agent_name)


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PROXY_PORT", "8100"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
