"""dg-patch — route discord.py through hermes-provisioner's Discord transport.

What it changes at import time:

1. REST calls (``discord.http.Route.BASE``) → the per-agent HTTP integration
   ``https://discord-<vm>.int.exe.xyz/api/v10``. exe.dev injects
   ``Authorization: Bot <real-token>`` server-side.

2. ``HTTPClient.request`` runs with ``self.token = None`` so the client
   never attaches an ``Authorization`` header itself. Auth is purely
   transport-layer; this VM has no bot token on it.

3. Gateway WebSocket (``DiscordWebSocket.from_client`` + sharded
   ``HTTPClient.get_bot_gateway``) mints a fresh single-use ticket per
   connect via ``platform-<vm>.int.exe.xyz/discord-gateway/ticket`` and
   connects to ``wss://discord-gateway.slate.ceo/tkt/<ticket>``. dg-proxy
   on sf1 rewrites the IDENTIFY frame with the real bot token.

4. Gateway compression forced off — dg-proxy doesn't handle zlib-stream.

5. RESUME forced off. discord.py's built-in reconnect logic tries RESUME
   (op 6) after a WS drop, re-sending the client's stored session_id +
   the placeholder token. dg-proxy only rewrites IDENTIFY (op 2), so a
   RESUME reaches Discord with the placeholder and gets close code 4004
   (auth failed). Forcing resume=False makes every reconnect a full
   IDENTIFY with a fresh ticket, which the proxy does rewrite.

6. Ticket minting retries transient failures (3×, 2s backoff) so a short
   provisioner blip doesn't kill a reconnect attempt.

Imported via a ``.pth`` file in the venv so these monkey-patches apply
before hermes's own imports of discord.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

try:
    import aiohttp
    import yarl
    import discord  # noqa: F401 — trigger submodule loads
    import discord.http
    import discord.gateway
except Exception:
    raise

log = logging.getLogger("dg-patch")

_VM = os.environ.get("DG_PATCH_VM") or socket.gethostname()
_HTTP_BASE = f"https://discord-{_VM}.int.exe.xyz/api/v10"
_TICKET_URL = f"https://platform-{_VM}.int.exe.xyz/discord-gateway/ticket"
_WS_PUBLIC_BASE = "wss://discord-gateway.slate.ceo"

# --- 1. REST base URL ------------------------------------------------------
discord.http.Route.BASE = _HTTP_BASE

# --- 2. Strip client-side Authorization. Transport-layer auth only. -------
_orig_request = discord.http.HTTPClient.request


async def _patched_request(self, route, *args, **kwargs):
    saved = self.token
    self.token = None
    try:
        return await _orig_request(self, route, *args, **kwargs)
    finally:
        self.token = saved


discord.http.HTTPClient.request = _patched_request


# --- 3. Gateway URL: mint a fresh ticket per connect, retry on blips ------
async def _mint_ws_url(retries: int = 3, backoff: float = 2.0) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(_TICKET_URL) as r:
                    r.raise_for_status()
                    body = await r.json()
            return body["ws_url"]
        except Exception as e:
            last_exc = e
            if attempt < retries:
                log.warning(
                    "dg-patch: ticket mint attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, retries, e, backoff,
                )
                await asyncio.sleep(backoff)
    assert last_exc is not None
    raise last_exc


_orig_from_client = discord.gateway.DiscordWebSocket.from_client.__func__


@classmethod
async def _patched_from_client(
    cls, client, *,
    gateway=None, compress=True, resume=False,
    session=None, sequence=None, **kw,
):
    # Force compress=False: dg-proxy does not decode zlib-stream.
    # Force resume=False + drop session/sequence: RESUME frames carry the
    # placeholder token through dg-proxy un-rewritten, causing Discord to
    # reply 4004 (auth failed). We re-IDENTIFY on every reconnect instead,
    # minting a fresh ticket each time.
    compress = False
    resume = False
    session = None
    sequence = None
    # Always mint a fresh ticket. Any gateway URL discord.py carries over
    # from a prior connect has an already-consumed ticket that dg-proxy
    # will reject (4401); reusing it gives us no value. Stateless URL per
    # from_client call.
    url = await _mint_ws_url()
    gateway = yarl.URL(url)
    return await _orig_from_client(
        cls, client,
        gateway=gateway, compress=compress, resume=resume,
        session=session, sequence=sequence, **kw,
    )


discord.gateway.DiscordWebSocket.from_client = _patched_from_client


# --- 4. Sharded clients use HTTPClient.get_bot_gateway for the gateway URL.
try:
    from discord.http import SessionStartLimit
except Exception:
    SessionStartLimit = None


async def _patched_get_bot_gateway(self):
    ws_url = await _mint_ws_url()
    if SessionStartLimit is not None:
        limits = SessionStartLimit(
            total=1000, remaining=1000,
            reset_after=86_400_000, max_concurrency=1,
        )
        return 1, ws_url, limits
    return 1, ws_url


discord.http.HTTPClient.get_bot_gateway = _patched_get_bot_gateway


async def _patched_get_gateway(self, *, encoding="json", zlib=False, v=10):
    return await _mint_ws_url()


discord.http.HTTPClient.get_gateway = _patched_get_gateway

# --- 5. Fallback DEFAULT_GATEWAY (rarely used; sanity only). --------------
discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(_WS_PUBLIC_BASE + "/")



log.warning(
    "dg-patch active vm=%s rest=%s ws=%s (resume disabled, ticket retry=3)",
    _VM, _HTTP_BASE, _WS_PUBLIC_BASE,
)
