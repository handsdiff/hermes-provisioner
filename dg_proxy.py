"""Discord gateway proxy. Terminates agent WebSocket, injects real bot token
into the outbound IDENTIFY frame, forwards everything else to Discord's
gateway unmodified. Preserves zero-secrets on the agent VM — the bot token
lives only in the server-side agents.db.

Auth: the agent presents a one-time ticket (?ticket=...) previously minted
by /discord-gateway/ticket on the provisioner API. Tickets are scoped to
(vm_name, service_name='discord'), 60s TTL, single-use.

Supported Discord gateway params passed through as-is: v, encoding.
Compression (?compress=zlib-stream) is forbidden in this MVP — we'd need to
decode the zlib stream to forward-pass correctly. The hermes-side adapter
opts out by not requesting compression.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets

sys.path.insert(0, str(Path(__file__).parent))
from db import consume_gateway_ticket, get_service_token  # noqa: E402

LISTEN_HOST = os.environ.get("DG_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("DG_PROXY_PORT", "8400"))
DISCORD_GATEWAY_HOST = "wss://gateway.discord.gg"
# Supported query param allowlist; everything else is dropped to avoid
# surprising Discord with compression etc.
ALLOWED_PARAMS = {"v", "encoding"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dg-proxy")
# Quiet the websockets library's own per-frame DEBUG stream (HANDSHAKE, < TEXT
# chunks, etc). We do our own frame-level logging below.
logging.getLogger("websockets").setLevel(logging.WARNING)


def _upstream_url(client_path: str) -> str:
    """Build the Discord upstream URL, passing through v + encoding only."""
    parsed = urlparse(client_path)
    qs = parse_qs(parsed.query)
    out = []
    for k in ALLOWED_PARAMS:
        if k in qs:
            out.append(f"{k}={qs[k][0]}")
    query = "&".join(out) if out else "v=10&encoding=json"
    return f"{DISCORD_GATEWAY_HOST}/?{query}"


async def _client_to_upstream(ws_client, ws_up, token, ctx):
    """Agent -> Discord. Rewrite IDENTIFY d.token in-flight."""
    async for raw in ws_client:
        if isinstance(raw, bytes):
            await ws_up.send(raw)
            continue
        try:
            msg = json.loads(raw)
        except Exception as e:
            log.warning("%s -> invalid JSON, dropping: %s", ctx, e)
            continue
        if msg.get("op") == 2:  # IDENTIFY
            if not isinstance(msg.get("d"), dict):
                log.warning("%s -> IDENTIFY without d object, dropping", ctx)
                continue
            msg["d"]["token"] = token
            msg["d"].pop("compress", None)
            raw = json.dumps(msg, separators=(",", ":"))
            log.info("%s -> IDENTIFY token rewritten", ctx)
        await ws_up.send(raw)


async def _upstream_to_client(ws_up, ws_client, ctx):
    """Discord -> Agent. Pass-through, logging only attention-worthy frames."""
    ready_logged = False
    async for raw in ws_up:
        if not isinstance(raw, bytes):
            try:
                msg = json.loads(raw)
                op = msg.get("op")
                t = msg.get("t")
                if op == 9:
                    log.warning("%s <- INVALID_SESSION resumable=%r", ctx, msg.get("d"))
                elif op == 0 and t == "READY" and not ready_logged:
                    user = (msg.get("d") or {}).get("user") or {}
                    log.info("%s <- READY user=%r id=%s", ctx,
                             user.get("username"), user.get("id"))
                    ready_logged = True
            except Exception:
                pass
        await ws_client.send(raw)


async def _handle(ws_client):
    """One agent connection. Validate ticket → open upstream → pump frames."""
    path = ws_client.request.path if hasattr(ws_client, "request") else getattr(ws_client, "path", "/")
    parsed = urlparse(path)
    # Accept either /tkt/<ticket>[/...] or ?ticket=<ticket>. The path form
    # survives yarl.URL.with_query() inside discord.py (which replaces the
    # query string with v=/encoding=); the query form is kept for
    # manual/scripted clients.
    ticket = ""
    p = parsed.path.strip("/")
    if p.startswith("tkt/"):
        ticket = p[len("tkt/"):].split("/")[0]
    if not ticket:
        qs = parse_qs(parsed.query)
        ticket = (qs.get("ticket", [""])[0] or "").strip()

    if not ticket:
        log.warning("connect rejected: missing ticket")
        await ws_client.close(code=4401, reason="missing ticket")
        return

    row = consume_gateway_ticket(ticket)
    if not row:
        log.warning("connect rejected: invalid/expired ticket")
        await ws_client.close(code=4401, reason="invalid or expired ticket")
        return

    vm = row["vm_name"]
    service = row["service_name"]
    ctx = f"[{vm}/{service}]"

    token = get_service_token(vm, service)
    if not token:
        log.error("%s no service token stored", ctx)
        await ws_client.close(code=4403, reason="no server-side token for service")
        return

    upstream_url = _upstream_url(path)
    log.info("%s opening upstream %s", ctx, upstream_url)
    try:
        async with websockets.connect(upstream_url, max_size=None) as ws_up:
            log.info("%s upstream connected", ctx)
            await asyncio.gather(
                _client_to_upstream(ws_client, ws_up, token, ctx),
                _upstream_to_client(ws_up, ws_client, ctx),
            )
    except websockets.ConnectionClosed as e:
        log.info("%s closed: %s", ctx, e)
    except Exception as e:
        log.exception("%s upstream error: %s", ctx, e)
    finally:
        log.info("%s session done", ctx)


async def main():
    log.info("dg-proxy listening on ws://%s:%s", LISTEN_HOST, LISTEN_PORT)
    async with websockets.serve(_handle, LISTEN_HOST, LISTEN_PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
