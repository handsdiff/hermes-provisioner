"""Hermes Provisioning API."""

import os
import re
import threading
import traceback

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from db import all_agents, get_agent, set_agent_status
from provision import provision_agent, destroy_agent

PROVISIONER_API_KEY = os.environ.get("PROVISIONER_API_KEY", "")
PROVISIONER_ADMIN_KEY = os.environ.get("PROVISIONER_ADMIN_KEY", "")

app = FastAPI()


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


def _validate_name(name: str):
    """Validate user-provided agent name."""
    if not name or len(name) > 46:
        raise HTTPException(status_code=400, detail="name must be 1-46 characters")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*", name):
        raise HTTPException(status_code=400, detail="name must start with a letter, contain only letters/digits/hyphens, no consecutive/trailing hyphens")


def _vm_name(name: str) -> str:
    """Derive exe.dev VM name from agent name (lowercase, min 5 chars)."""
    vm = name.lower()
    if len(vm) < 5:
        vm = f"slate-{vm}"
    return vm


def _provision_background(name, vm_name, display_name, email, telegram_bot_token, telegram_username):
    """Run provisioning in a background thread, updating DB status."""
    try:
        provision_agent(name, email, telegram_bot_token, telegram_username,
                        display_name=display_name, vm_name=vm_name)
        set_agent_status(name, "ready")
    except Exception as e:
        set_agent_status(name, "failed", str(e))
        traceback.print_exc()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/agents")
def create_agent(
    agent_name: str,
    owner_email: str,
    telegram_bot_token: str,
    owner_telegram_username: str,
    x_api_key: str = Header(None, alias="X-Api-Key"),
):
    _check_auth(x_api_key)
    _validate_name(agent_name)
    name = agent_name.lower()
    vm_name = _vm_name(agent_name)
    if get_agent(name):
        raise HTTPException(status_code=409, detail=f"Agent '{name}' already exists")
    thread = threading.Thread(
        target=_provision_background,
        args=(name, vm_name, agent_name, owner_email, telegram_bot_token, owner_telegram_username),
        daemon=True,
    )
    thread.start()
    return JSONResponse(
        {"agent_name": agent_name, "name": name, "vm_name": vm_name, "status": "provisioning"},
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
    result = {"name": name, "vm_name": vm, "status": agent.get("status", "ready")}
    if agent.get("error"):
        result["error"] = agent["error"]
    if result["status"] == "ready":
        result["url"] = f"https://{vm}.exe.xyz"
        result["shelley"] = f"https://{vm}.shelley.exe.xyz/"
        result["ssh"] = f"ssh {vm}.exe.xyz"
    return JSONResponse(result)


@app.get("/agents")
def list_agents(x_api_key: str = Header(None, alias="X-Api-Key")):
    _check_auth(x_api_key)
    agents = all_agents()
    return JSONResponse({
        name: {"name": name, "status": info.get("status", "ready")}
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


if __name__ == "__main__":
    port = int(os.environ.get("PROVISIONER_PORT", "8200"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
