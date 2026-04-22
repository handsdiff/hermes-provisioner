"""Hermes Provisioning API."""

import html
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
import traceback

import uvicorn
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from db import (
    all_agents,
    all_humans,
    get_agent,
    get_credential_request,
    mark_credential_request_used,
    public_agent_info,
    save_credential_request,
    save_service_token,
    set_agent_status,
    vm_for_agent_secret,
)
from provision import (
    prepare_agent,
    provision_agent,
    destroy_agent,
    update_agent,
    vm_tags_from_exe,
    write_integrations_manifest,
)

PROVISIONER_API_KEY = os.environ.get("PROVISIONER_API_KEY", "")
PROVISIONER_ADMIN_KEY = os.environ.get("PROVISIONER_ADMIN_KEY", "")
CREATION_API_KEY = os.environ.get("CREATION_API_KEY", "")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://slate.ceo",
        "https://slate-sal.exe.xyz",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Api-Key", "Content-Type"],
)


def _check_auth(api_key: str | None):
    if not PROVISIONER_API_KEY:
        raise HTTPException(status_code=500, detail="PROVISIONER_API_KEY not configured")
    if api_key != PROVISIONER_API_KEY and api_key != PROVISIONER_ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _check_admin(api_key: str | None):
    if not PROVISIONER_ADMIN_KEY:
        raise HTTPException(status_code=500, detail="PROVISIONER_ADMIN_KEY not configured")
    if api_key != PROVISIONER_ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Admin API key required")


def _check_creation(api_key: str | None):
    if not CREATION_API_KEY:
        raise HTTPException(status_code=500, detail="CREATION_API_KEY not configured")
    if api_key != CREATION_API_KEY and api_key != PROVISIONER_ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Creation API key required")


def _validate_name(name: str):
    """Validate user-provided agent name."""
    if not name or len(name) > 46:
        raise HTTPException(status_code=400, detail="name must be 1-46 characters")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*", name):
        raise HTTPException(status_code=400, detail="name must start with a letter, contain only letters/digits/hyphens, no consecutive/trailing hyphens")


def _vm_name(name: str) -> str:
    """Derive exe.dev VM name from agent name.

    Always prefixed with `slate-` to keep VM names globally unique and
    sidestep exe.dev's reserved-name collisions (e.g. `andrew` was rejected).
    If the agent name already starts with `slate-`, it's returned as-is.
    """
    vm = name.lower()
    if not vm.startswith("slate-"):
        vm = f"slate-{vm}"
    return vm


def _provision_background(name, email, vm_name, display_name, prep):
    """Run VM provisioning in a background thread, updating DB status."""
    try:
        provision_agent(name, email, vm_name, display_name, prep)
        set_agent_status(name, "ready")
    except Exception as e:
        set_agent_status(name, "failed", str(e))
        traceback.print_exc()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/agent/environment")
def agent_environment(
    x_agent_secret: str = Header(None, alias="X-Agent-Secret"),
):
    """Server-rendered SOUL environment block for the calling VM.

    Auth: X-Agent-Secret header (the same secret injected by the
    `platform-<vm>` exe.dev integration). A VM can only fetch its own
    environment — this lets the roster be fresh without making peer
    rosters globally enumerable by anyone who guesses a vm_name.

    Computed from current DB state on every request — so changes to the
    agents table (new peers, updated owner descriptions, renamed bots)
    are reflected immediately. VMs pull this via a 15-minute systemd
    timer and splice it into their SOUL.md between auto-gen markers.

    Response: text/markdown. Returns 401 on missing/bad secret, 404 if
    the agent's row has been removed.
    """
    from fastapi.responses import PlainTextResponse
    from env_block import render_for_vm
    if not x_agent_secret:
        raise HTTPException(status_code=401, detail="X-Agent-Secret header required")
    vm_name = vm_for_agent_secret(x_agent_secret)
    if not vm_name:
        raise HTTPException(status_code=401, detail="Invalid X-Agent-Secret")
    try:
        block = render_for_vm(vm_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return PlainTextResponse(block, media_type="text/markdown")


@app.get("/humans")
def list_humans():
    """Platform roster: humans on the platform and their hub agents.

    Unauthenticated — roster is the set of human owners of provisioned agents,
    who already consent to being reachable through their agent. Agents call
    this to discover who they can route messages to.
    """
    return JSONResponse({"humans": all_humans()})


@app.post("/agents")
def create_agent(
    agent_name: str,
    owner_email: str,
    discord_username: str,
    owner_description: str = "",
    x_api_key: str = Header(None, alias="X-Api-Key"),
):
    """Create an agent. `owner_description` is the free-text answer to the
    onboarding intake ("what would you love your agent to do?"). Used to
    seed the agent's SOUL.md with direction and surfaced to peer agents
    via /humans so they can describe each other to their owners.
    """
    _check_creation(x_api_key)
    _validate_name(agent_name)
    name = agent_name.lower()
    vm_name = _vm_name(agent_name)
    if get_agent(name):
        raise HTTPException(status_code=409, detail=f"Agent '{name}' already exists")

    owner_description = (owner_description or "").strip()
    if len(owner_description) > 600:
        raise HTTPException(status_code=400, detail="owner_description must be ≤ 600 chars")

    # Synchronous pre-checks: resolve Discord username, claim bot from
    # pool + rename it, register on Hub. Any failure (owner not in Slate
    # Discord, empty bot pool, rename rate-limit) returns immediately.
    try:
        prep = prepare_agent(
            name, owner_email, discord_username,
            display_name=agent_name, vm_name=vm_name,
            owner_description=owner_description,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # VM creation + setup runs in background
    thread = threading.Thread(
        target=_provision_background,
        args=(name, owner_email, vm_name, agent_name, prep),
        daemon=True,
    )
    thread.start()
    client_id = prep["bot_client_id"]
    return JSONResponse(
        {
            "agent_name": agent_name,
            "name": name,
            "vm_name": vm_name,
            "status": "provisioning",
            "dm_url": f"https://discord.com/users/{client_id}",
            "oauth_url": f"https://discord.com/oauth2/authorize?client_id={client_id}",
        },
        status_code=202,
    )


@app.get("/agents/{name}")
def get_agent_status(name: str, x_api_key: str = Header(None, alias="X-Api-Key")):
    _check_auth(x_api_key)
    name = name.lower()
    agent = get_agent(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    vm = _vm_name(name)
    result = public_agent_info(agent)
    result["vm_name"] = vm
    if result.get("status") == "ready":
        result["url"] = f"https://{vm}.exe.xyz"
        result["shelley"] = f"https://{vm}.shelley.exe.xyz/"
        result["ssh"] = f"ssh {vm}.exe.xyz"
    return JSONResponse(result)


@app.get("/agents")
def list_agents(x_api_key: str = Header(None, alias="X-Api-Key")):
    _check_auth(x_api_key)
    agents = all_agents()
    return JSONResponse({
        name: public_agent_info(info)
        for name, info in agents.items()
    })


@app.delete("/agents/{name}")
def delete_agent_endpoint(name: str, x_api_key: str = Header(None, alias="X-Api-Key")):
    _check_admin(x_api_key)
    name = name.lower()
    if not get_agent(name):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    try:
        result = destroy_agent(_vm_name(name))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    from db import delete_agent as db_delete_agent
    db_delete_agent(name)
    return JSONResponse(result)


def _update_fleet_background(agents):
    """Update all agents in background, printing results."""
    for name, info in agents.items():
        if info.get("status") != "ready":
            print(f"  Skipping {name} (status: {info.get('status')})")
            continue
        vm = _vm_name(name)
        result = update_agent(vm)
        if result["status"] == "failed":
            print(f"  FAILED {name}: {result['error']}")


@app.post("/agents/update")
def update_fleet(x_api_key: str = Header(None, alias="X-Api-Key")):
    """Update hermes-agent code on all ready agents. Admin key required."""
    _check_admin(x_api_key)
    agents = all_agents()
    ready = {n: a for n, a in agents.items() if a.get("status") == "ready"}
    if not ready:
        return JSONResponse({"ok": True, "message": "No ready agents to update"})
    thread = threading.Thread(
        target=_update_fleet_background,
        args=(ready,),
        daemon=True,
    )
    thread.start()
    return JSONResponse(
        {"ok": True, "updating": list(ready.keys()), "count": len(ready)},
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Layer 0 — self-serve integration flow
#
# Agent calls POST /integrations/request (authenticated via the per-agent
# platform-<vm> integration that injects X-Agent-Secret). The response
# contains a one-time URL the agent hands to its owner in chat. The owner
# clicks, pastes the credential into a form, submits. The form POST creates
# an exe.dev integration scoped to that agent's VM.
#
# Nothing here stores the credential: the paste goes directly into the
# `integrations add` CLI call. The VM still holds zero secrets.
# ---------------------------------------------------------------------------

SETUP_PUBLIC_BASE = os.environ.get("SETUP_PUBLIC_BASE", "https://provision.slate.ceo")
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
_AUTH_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,19}$")
_CRED_REQUEST_TTL = 900  # 15 minutes

# Services that need a gateway proxy (not just HTTP Bearer injection). For
# these, the Layer 0 submit also stashes the raw token in
# agent_service_tokens and creates a per-VM `dg-<vm>` integration so
# dg-proxy can authenticate the agent and rewrite IDENTIFY frames.
_GATEWAY_PROXIED_SERVICES = {"discord"}


def _validate_service_name(service_name: str) -> str:
    service_name = (service_name or "").lower().strip()
    if not _SERVICE_NAME_RE.fullmatch(service_name):
        raise HTTPException(
            status_code=400,
            detail="service_name must be 1-20 chars, lowercase alphanumeric + hyphens, starting with letter/digit",
        )
    return service_name


def _validate_target_url(target_url: str) -> str:
    target_url = (target_url or "").strip()
    if not target_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="target_url must start with https://")
    # exe.dev requires scheme + host only (no path). Match the existing rule
    # documented in README.md → exe.dev gotchas.
    without_scheme = target_url[len("https://"):]
    if "/" in without_scheme.rstrip("/"):
        raise HTTPException(
            status_code=400,
            detail="target_url must be scheme + host only (no path)",
        )
    return target_url.rstrip("/")


@app.post("/integrations/request")
async def request_credential(
    request: Request,
    x_agent_secret: str = Header(None, alias="X-Agent-Secret"),
):
    """Agent-authenticated. Mint a one-time setup URL for a missing credential.

    Invoked via the per-agent platform-<vm> exe.dev integration which injects
    the X-Agent-Secret header. The agent shares the returned setup_url with
    its owner in chat; the owner clicks through to paste the credential.
    """
    if not x_agent_secret:
        raise HTTPException(status_code=401, detail="X-Agent-Secret header required")
    vm_name = vm_for_agent_secret(x_agent_secret)
    if not vm_name:
        raise HTTPException(status_code=401, detail="Unknown agent secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON")
    service_name = _validate_service_name(body.get("service_name", ""))
    target_url = _validate_target_url(body.get("target_url", ""))
    description = (body.get("description") or "").strip()[:500]
    auth_scheme = (body.get("auth_scheme") or "Bearer").strip()
    if not _AUTH_SCHEME_RE.fullmatch(auth_scheme):
        raise HTTPException(
            status_code=400,
            detail="auth_scheme must be letters/digits/hyphens only",
        )

    token = secrets.token_urlsafe(24)
    expires_at = save_credential_request(
        token, vm_name, service_name, target_url, description,
        ttl_seconds=_CRED_REQUEST_TTL,
    )
    # Stash auth_scheme on the row (column added via ALTER TABLE migration).
    import sqlite3
    from db import DB_PATH
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE credential_requests SET auth_scheme = ? WHERE token = ?",
                (auth_scheme, token))
    con.commit()
    con.close()
    return JSONResponse({
        "setup_url": f"{SETUP_PUBLIC_BASE}/integrations/setup/{token}",
        "expires_at": expires_at,
        "service_name": service_name,
        "vm_name": vm_name,
        "auth_scheme": auth_scheme,
    })


def _render_setup_page(req: dict, error: str | None = None) -> str:
    service = html.escape(req["service_name"])
    target = html.escape(req["target_url"])
    desc = html.escape(req["description"] or "(no description)")
    vm = html.escape(req["vm_name"])
    err_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Set up {service} for {vm}</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:560px;margin:4em auto;padding:0 1em;color:#222}}
 h1{{font-size:1.4em}}
 dl{{background:#f5f5f5;padding:1em;border-radius:6px}}
 dt{{font-weight:600;margin-top:.4em}}
 dd{{margin:0 0 .3em}}
 input[type=text]{{width:100%;padding:.6em;font-family:monospace;font-size:1em;box-sizing:border-box}}
 button{{margin-top:1em;padding:.6em 1.2em;font-size:1em;cursor:pointer}}
 .err{{color:#b00;background:#fee;padding:.6em;border-radius:4px}}
 .note{{font-size:.85em;color:#666;margin-top:1.5em}}
</style></head>
<body>
<h1>Grant <code>{service}</code> access to agent <code>{vm}</code></h1>
<p>Paste the credential below. It goes directly to the platform's
integration layer — not into any chat log or agent memory.</p>
<dl>
  <dt>Agent</dt><dd><code>{vm}</code></dd>
  <dt>Service</dt><dd><code>{service}</code></dd>
  <dt>Target URL</dt><dd><code>{target}</code></dd>
  <dt>What the agent says it's for</dt><dd>{desc}</dd>
</dl>
{err_html}
<form method="POST" autocomplete="off">
  <label for="credential"><strong>Credential</strong> (API key, token, etc.):</label><br>
  <input type="text" id="credential" name="credential" autocomplete="off" spellcheck="false" required>
  <button type="submit">Grant access</button>
</form>
<p class="note">This link is single-use and expires in 15 minutes. The
credential will be injected by the platform as
<code>Authorization: Bearer &lt;value&gt;</code>.</p>
</body></html>"""


def _render_result_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:560px;margin:4em auto;padding:0 1em;color:#222}}
 h1{{font-size:1.4em}}
 .ok{{color:#060}}
 .err{{color:#b00}}
</style></head><body>{body}</body></html>"""


def _token_validity_error(req: dict | None) -> str | None:
    if not req:
        return "Unknown or revoked setup link."
    if req.get("used_at"):
        return "This setup link has already been used."
    if int(time.time()) >= req["expires_at"]:
        return "This setup link has expired. Ask the agent for a new one."
    return None


@app.get("/integrations/setup/{token}", response_class=HTMLResponse)
def integrations_setup_form(token: str):
    req = get_credential_request(token)
    err = _token_validity_error(req)
    if err:
        return HTMLResponse(
            _render_result_page("Setup link unavailable",
                                f'<h1 class="err">Setup link unavailable</h1><p>{html.escape(err)}</p>'),
            status_code=410,
        )
    return HTMLResponse(_render_setup_page(req))


@app.post("/integrations/setup/{token}", response_class=HTMLResponse)
def integrations_setup_submit(token: str, credential: str = Form(...)):
    req = get_credential_request(token)
    err = _token_validity_error(req)
    if err:
        return HTMLResponse(
            _render_result_page("Setup link unavailable",
                                f'<h1 class="err">Setup link unavailable</h1><p>{html.escape(err)}</p>'),
            status_code=410,
        )
    credential = (credential or "").strip()
    if not credential:
        return HTMLResponse(
            _render_setup_page(req, error="Paste the credential before submitting."),
            status_code=400,
        )
    # Single-use: mark before the side effect. If the CLI call fails, the
    # user can ask the agent for a fresh link — cheaper than risking a
    # double-create.
    if not mark_credential_request_used(token):
        return HTMLResponse(
            _render_result_page("Setup link unavailable",
                                '<h1 class="err">Already used</h1><p>This link was used by another tab.</p>'),
            status_code=410,
        )

    vm = req["vm_name"]
    service = req["service_name"]
    integration_name = f"{service}-{vm}"
    auth_scheme = (req.get("auth_scheme") or "Bearer").strip() or "Bearer"
    # `ssh exe.dev <argv...>` concatenates and re-parses on the remote side,
    # so credential/target (which may contain shell-sensitive chars) need
    # remote-shell quoting. service/vm/integration_name are already
    # validated to [a-z0-9-] + fixed shape, but quote uniformly for safety.
    if auth_scheme.lower() == "bearer":
        auth_arg = shlex.quote(f"--bearer={credential}")
    else:
        auth_arg = shlex.quote(f"--header=Authorization:{auth_scheme} {credential}")
    remote_cmd = " ".join([
        "integrations", "add", "http-proxy",
        shlex.quote(f"--name={integration_name}"),
        shlex.quote(f"--target={req['target_url']}"),
        auth_arg,
        shlex.quote(f"--attach=vm:{vm}"),
    ])
    try:
        result = subprocess.run(
            ["ssh", "exe.dev", remote_cmd],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return HTMLResponse(
            _render_result_page("Setup failed",
                                '<h1 class="err">Setup timed out</h1><p>The platform didn\'t respond within 30 seconds. Ask the agent for a new link and try again.</p>'),
            status_code=504,
        )
    if result.returncode != 0:
        err_text = (result.stderr or result.stdout or "unknown error").strip()
        # Don't echo the whole stderr — it may contain details we'd rather
        # not render. Pattern-match the common cases.
        msg = "The platform rejected the integration."
        if "already exists" in err_text.lower() or "duplicate" in err_text.lower():
            msg = f"An integration named <code>{html.escape(integration_name)}</code> already exists. Ask the agent to pick a different service name."
        return HTMLResponse(
            _render_result_page("Setup failed",
                                f'<h1 class="err">Setup failed</h1><p>{msg}</p>'),
            status_code=502,
        )
    # For gateway-proxied services (Discord etc), also stash the raw token
    # server-side so dg-proxy can rewrite IDENTIFY frames. The agent still
    # never sees it. Do this before manifest refresh — if it fails, the
    # token is lost and the user would have to mint a new setup link.
    if service in _GATEWAY_PROXIED_SERVICES:
        try:
            save_service_token(vm, service, credential)
        except Exception as e:
            traceback.print_exc()
            print(f"service-token save failed for {vm}/{service}: {e}")
        # Also create the per-VM `dg-<vm>` exe.dev integration that
        # proxies the WebSocket connect to dg-proxy with X-Agent-Secret
        # injected. Agent never sees the secret; dg-proxy validates the
        # header and looks up the bot token server-side. Idempotent — if
        # the integration already exists (re-paste), we skip.
        try:
            from db import agent_secret_for_vm
            agent_secret = agent_secret_for_vm(vm)
            if agent_secret:
                dg_name = f"dg-{vm}"
                dg_cmd = " ".join([
                    "integrations", "add", "http-proxy",
                    shlex.quote(f"--name={dg_name}"),
                    shlex.quote("--target=https://discord-gateway.slate.ceo"),
                    shlex.quote(f"--header=X-Agent-Secret:{agent_secret}"),
                    shlex.quote(f"--attach=vm:{vm}"),
                ])
                dg_result = subprocess.run(
                    ["ssh", "exe.dev", dg_cmd],
                    capture_output=True, text=True, timeout=30,
                )
                if dg_result.returncode != 0:
                    err_text = (dg_result.stderr or dg_result.stdout or "").lower()
                    if "already exists" not in err_text and "duplicate" not in err_text:
                        print(f"dg integration create failed for {vm}: "
                              f"{(dg_result.stderr or dg_result.stdout).strip()}")
            else:
                print(f"no agent_secret for {vm}; skipping dg-{vm} integration")
        except Exception as e:
            traceback.print_exc()
            print(f"dg integration setup failed for {vm}: {e}")
    # Best-effort: refresh the VM's integrations.json so the agent sees
    # the new capability on its next `integrations list` call. Don't fail
    # the request if this doesn't work — the integration is already live;
    # worst case the agent has a stale manifest until next provision.
    try:
        tags = vm_tags_from_exe(vm)
        write_integrations_manifest(vm, vm_tags=tags)
    except Exception as e:
        traceback.print_exc()
        print(f"manifest refresh failed for {vm}: {e}")
    return HTMLResponse(_render_result_page(
        "Setup complete",
        f'<h1 class="ok">Access granted</h1>'
        f'<p>Agent <code>{html.escape(vm)}</code> now has access to <code>{html.escape(service)}</code>. It can call <code>{html.escape(req["target_url"])}</code> through the platform proxy on its next turn — you can close this tab.</p>',
    ))


if __name__ == "__main__":
    port = int(os.environ.get("PROVISIONER_PORT", "8200"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
