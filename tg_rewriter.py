"""Telegram URL rewriter — moves bot token from header to URL path.

Per-agent exe.dev integrations inject X-Bot-Token as a header. Telegram's
Bot API requires the token in the URL path (/bot<token>/method). This
service bridges the gap.

The python-telegram-bot library sends requests as:
  {base_url}/bot{dummy_token}/{method}
  {base_file_url}/file/bot{dummy_token}/{path}

The rewriter strips the dummy bot{...}/ prefix from the incoming path and
rebuilds it with the real token from the X-Bot-Token header.

No auth, no database. If X-Bot-Token is missing, the request didn't come
through a properly configured integration.

Run: python3 tg_rewriter.py (port 8100 default, behind proxy.slate.ceo)
"""

import os
import re

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TELEGRAM_API = "https://api.telegram.org"
# Matches: {dummy_token}/{method} or file/{dummy_token}/{path}
# The dummy token is the first path segment (whatever the library sends as "token")
DUMMY_TOKEN_RE = re.compile(r"^(file/)?[^/]+/(.*)")

app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def rewrite(request: Request, path: str):
    token = request.headers.get("X-Bot-Token", "")
    if not token:
        return JSONResponse({"ok": False, "error": "Missing X-Bot-Token header"}, status_code=400)

    # Strip dummy token from path, rebuild with real token
    # Library sends: {base_url}/{token}/{method} → path = "{dummy_token}/{method}"
    # File downloads: {base_file_url}/file/{token}/{path} → path = "file/{dummy_token}/{path}"
    m = DUMMY_TOKEN_RE.match(path)
    if m:
        file_prefix = m.group(1) or ""  # "file/" or ""
        rest = m.group(2)               # the actual method/path
        url = f"{TELEGRAM_API}/{file_prefix}bot{token}/{rest}"
    else:
        # Single segment (no slash) — treat as method name
        url = f"{TELEGRAM_API}/bot{token}/{path}"

    if request.url.query:
        url += f"?{request.url.query}"

    headers = {k: v for k, v in request.headers.items()
                if k.lower() not in ("host", "x-bot-token", "content-length")}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(
            method=request.method, url=url,
            headers=headers, content=await request.body(),
        )

    return Response(content=resp.content, status_code=resp.status_code,
                    headers=dict(resp.headers))


if __name__ == "__main__":
    port = int(os.environ.get("TG_REWRITER_PORT", "8100"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
