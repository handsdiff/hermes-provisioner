"""Hermes Provisioning API."""

import os
import re

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from db import get_agent, all_agents
from provision import provision_agent, destroy_agent

PROVISIONER_API_KEY = os.environ.get("PROVISIONER_API_KEY", "")

app = FastAPI()


def _check_auth(api_key: str | None):
    if not PROVISIONER_API_KEY:
        raise HTTPException(status_code=500, detail="PROVISIONER_API_KEY not configured")
    if api_key != PROVISIONER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _validate_name(name: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise HTTPException(status_code=400, detail="name must be alphanumeric, hyphens, or underscores only")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/agents")
def create_agent(
    name: str,
    email: str,
    telegram_bot_token: str = "",
    telegram_username: str = "",
    x_api_key: str = Header(None, alias="X-Api-Key"),
):
    _check_auth(x_api_key)
    _validate_name(name)
    if get_agent(name):
        raise HTTPException(status_code=409, detail=f"Agent '{name}' already exists")
    try:
        result = provision_agent(name, email, telegram_bot_token, telegram_username)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(result, status_code=201)


@app.get("/agents")
def list_agents(x_api_key: str = Header(None, alias="X-Api-Key")):
    _check_auth(x_api_key)
    agents = all_agents()
    return JSONResponse({name: {"name": name} for name in agents})


@app.get("/agents/{name}")
def get_agent_info(name: str, x_api_key: str = Header(None, alias="X-Api-Key")):
    _check_auth(x_api_key)
    agent = get_agent(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return JSONResponse({"name": agent["name"]})


@app.delete("/agents/{name}")
def delete_agent_endpoint(name: str, x_api_key: str = Header(None, alias="X-Api-Key")):
    _check_auth(x_api_key)
    _validate_name(name)
    if not get_agent(name):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    try:
        result = destroy_agent(name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(result)


if __name__ == "__main__":
    port = int(os.environ.get("PROVISIONER_PORT", "8200"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
