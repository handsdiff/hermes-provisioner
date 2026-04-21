"""dg-patch — route discord.py through hermes-provisioner's Discord transport.

What it changes at import time:

1. REST calls (``discord.http.Route.BASE``) → the per-agent HTTP integration
   ``https://discord-<vm>.int.exe.xyz/api/v10``. exe.dev injects
   ``Authorization: Bot <real-token>`` server-side.

2. ``HTTPClient.request`` runs with ``self.token = None`` so the client
   never attaches an ``Authorization`` header itself. Auth is purely
   transport-layer; this VM has no bot token on it.

3. Gateway WebSocket URL → ``wss://dg-<vm>.int.exe.xyz/``. The agent's
   per-VM ``dg-<vm>`` exe.dev integration injects ``X-Agent-Secret`` on
   the WS upgrade request; dg-proxy on sf1 validates the header and
   rewrites the IDENTIFY frame with the real bot token. No credentials
   or capabilities ever touch the agent VM — same model as the REST path.

4. Gateway compression forced off — dg-proxy doesn't handle zlib-stream.

5. RESUME forced off. discord.py's built-in reconnect logic tries RESUME
   (op 6) after a WS drop, re-sending the client's stored session_id +
   the placeholder token. dg-proxy only rewrites IDENTIFY (op 2), so a
   RESUME reaches Discord with the placeholder and gets close code 4004
   (auth failed). Forcing resume=False makes every reconnect a full
   IDENTIFY, which the proxy does rewrite.

Imported via a ``.pth`` file in the venv so these monkey-patches apply
before hermes's own imports of discord.py.
"""

from __future__ import annotations

import logging
import os
import socket

try:
    import yarl
    import discord  # noqa: F401 — trigger submodule loads
    import discord.http
    import discord.gateway
except Exception:
    raise

log = logging.getLogger("dg-patch")

_VM = os.environ.get("DG_PATCH_VM") or socket.gethostname()
_HTTP_BASE = f"https://discord-{_VM}.int.exe.xyz/api/v10"
_WS_URL = f"wss://dg-{_VM}.int.exe.xyz/"

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


# --- 3. Gateway URL: always the per-VM integration. ------------------------
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
    # reply 4004 (auth failed). We re-IDENTIFY on every reconnect instead.
    compress = False
    resume = False
    session = None
    sequence = None
    gateway = yarl.URL(_WS_URL)
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
    if SessionStartLimit is not None:
        limits = SessionStartLimit(
            total=1000, remaining=1000,
            reset_after=86_400_000, max_concurrency=1,
        )
        return 1, _WS_URL, limits
    return 1, _WS_URL


discord.http.HTTPClient.get_bot_gateway = _patched_get_bot_gateway


async def _patched_get_gateway(self, *, encoding="json", zlib=False, v=10):
    return _WS_URL


discord.http.HTTPClient.get_gateway = _patched_get_gateway

# --- 5. Fallback DEFAULT_GATEWAY (rarely used; sanity only). --------------
discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(_WS_URL)


log.warning(
    "dg-patch active vm=%s rest=%s ws=%s (header auth, resume disabled)",
    _VM, _HTTP_BASE, _WS_URL,
)
