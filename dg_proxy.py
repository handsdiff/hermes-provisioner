"""Discord gateway proxy. Terminates agent WebSocket, injects real bot token
into the outbound IDENTIFY frame, forwards everything else to Discord's
gateway unmodified. Preserves zero-secrets on the agent VM — the bot token
lives only in the server-side agents.db.

Auth: the agent's per-VM `dg-<vm>.int.exe.xyz` exe.dev integration injects
`X-Agent-Secret: <secret>` on the WS upgrade request. dg-proxy looks up the
(vm, 'discord') pair and fetches the bot token for IDENTIFY rewrite. The
agent VM itself never handles any credential — same model as the REST path.

Supported Discord gateway params passed through as-is: v, encoding.
Compression (?compress=zlib-stream) is forbidden in this MVP — we'd need to
decode the zlib stream to forward-pass correctly. The hermes-side adapter
opts out by not requesting compression.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets

sys.path.insert(0, str(Path(__file__).parent))
from db import get_service_token, vm_for_agent_secret  # noqa: E402

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


# Tracks live client connections so we can drain them on SIGTERM. discord.py's
# reconnect logic treats any non-1000 close (the default for an abrupt proxy
# exit) as terminal; closing each client with 1000 makes it fall through to
# its retry path instead.
_live_clients: set = set()


async def _handle(ws_client):
    """One agent connection. Authenticate → open upstream → pump frames.

    Auth: `X-Agent-Secret` header injected by the agent's per-VM
    `dg-<vm>.int.exe.xyz` exe.dev integration.
    """
    headers = ws_client.request.headers if hasattr(ws_client, "request") else {}
    agent_secret = (
        headers.get("X-Agent-Secret")
        or headers.get("x-agent-secret")
        or ""
    ).strip()
    path = ws_client.request.path if hasattr(ws_client, "request") else getattr(ws_client, "path", "/")

    if not agent_secret:
        log.warning("connect rejected: missing X-Agent-Secret")
        await ws_client.close(code=4401, reason="missing X-Agent-Secret")
        return

    vm = vm_for_agent_secret(agent_secret)
    if not vm:
        log.warning("connect rejected: unknown X-Agent-Secret")
        await ws_client.close(code=4401, reason="unknown X-Agent-Secret")
        return

    service = "discord"
    ctx = f"[{vm}/{service}]"

    token = get_service_token(vm, service)
    if not token:
        log.error("%s no service token stored", ctx)
        await ws_client.close(code=4403, reason="no server-side token for service")
        return

    upstream_url = _upstream_url(path)
    log.info("%s opening upstream %s", ctx, upstream_url)
    _live_clients.add(ws_client)
    try:
        # ping_interval=None: let Discord's own gateway heartbeat (in-band
        # WS frames) be the sole keepalive. The websockets library's
        # separate WS ping/pong fires during long LLM calls (hermes blocks
        # the event loop for tens of seconds on slow model turns) and
        # spuriously tears down the connection.
        async with websockets.connect(upstream_url, max_size=None, ping_interval=None) as ws_up:
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
        _live_clients.discard(ws_client)
        log.info("%s session done", ctx)


async def main():
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)
    loop.add_signal_handler(signal.SIGINT, shutdown.set)

    # ping_interval=None: see comment on websockets.connect below — hermes
    # blocks the event loop on LLM calls, and the default 20s ping/pong
    # timeout tears down otherwise-healthy connections.
    server = await websockets.serve(
        _handle, LISTEN_HOST, LISTEN_PORT,
        max_size=None, ping_interval=None,
    )
    log.info("dg-proxy listening on ws://%s:%s", LISTEN_HOST, LISTEN_PORT)
    await shutdown.wait()

    # Graceful drain: close each live client with a spec-clean 1000 so
    # discord.py's Client.connect() loop takes its retry path instead of
    # killing the adapter task. Websockets 1006 / no-close-frame (what a
    # hard exit produces) is treated as terminal by discord.py 2.x.
    log.info("shutdown signal; draining %d live connection(s)", len(_live_clients))
    closers = [
        ws.close(code=1000, reason="dg-proxy restart")
        for ws in list(_live_clients)
    ]
    if closers:
        await asyncio.gather(*closers, return_exceptions=True)
    server.close()
    await server.wait_closed()
    log.info("dg-proxy exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
