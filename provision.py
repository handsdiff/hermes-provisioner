#!/usr/bin/env python3
"""Create an exe.dev VM running a Hermes agent with inference, browser, Hub, and Shelley access."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

TAG = "slate-1"
HUB_BASE_URL = "http://127.0.0.1:34813"  # Hub on localhost, avoid Cloudflare
from db import save_agent as db_save_agent
TG_SESSION_PATH = str(Path("/opt/spice/prod/devops/session"))
TG_API_ID = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")


def run(cmd, *, check=True, capture=True, timeout=60, input=None):
    """Run a command, return stdout."""
    r = subprocess.run(
        cmd, shell=isinstance(cmd, str),
        capture_output=capture, text=True, timeout=timeout, input=input,
    )
    if check and r.returncode != 0:
        msg = f"Command failed: {cmd}"
        if r.stderr:
            msg += f"\n{r.stderr}"
        raise RuntimeError(msg)
    return r.stdout.strip() if capture else None


def wait_for_ssh(name, retries=30, delay=2):
    """Wait until SSH to the VM works."""
    for i in range(retries):
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
                 f"{name}.exe.xyz", "echo ok"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        if i < retries - 1:
            print(f"  waiting for SSH... ({i+1}/{retries})")
            time.sleep(delay)
    return False


def ssh_vm(name, script, timeout=300):
    """Run a script on the VM via SSH."""
    return run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"{name}.exe.xyz", "bash -ls"],
        input=script, timeout=timeout,
    )


def register_hub_agent(agent_id, description="", capabilities=None):
    """Register an agent on Hub. Returns (agent_id, secret)."""
    payload = {"agent_id": agent_id}
    if description:
        payload["description"] = description
    if capabilities:
        payload["capabilities"] = capabilities
    resp = httpx.post(
        f"{HUB_BASE_URL}/agents/register",
        json=payload,
        timeout=30.0,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Hub registration failed: {data}")
    return data["agent_id"], data["secret"]


def save_agent_credentials(name, hub_secret, hub_proxy_token, tg_proxy_token="",
                           telegram_bot_token=""):
    """Save agent credentials to the database for the proxy."""
    db_save_agent(name, hub_secret, hub_proxy_token, tg_proxy_token,
                  telegram_bot_token)


async def resolve_telegram_user(username: str) -> tuple[int, str]:
    """Resolve a @username to (numeric_id, first_name) via Telethon."""
    from telethon import TelegramClient
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH not configured")
    client = TelegramClient(TG_SESSION_PATH, TG_API_ID, TG_API_HASH)
    await client.connect()
    try:
        entity = await client.get_entity(username)
        return entity.id, (entity.first_name or "").split(" ")[0]
    finally:
        await client.disconnect()


SETUP_SCRIPT = (Path(__file__).parent / "setup.sh").read_text()


def provision_agent(name, email, telegram_bot_token="", telegram_username=""):
    """Provision a Hermes agent on exe.dev. Returns result dict."""
    # 1. Register agent on Hub
    print("Registering agent on Hub...")
    hub_agent_id, hub_secret = register_hub_agent(
        name,
        description=f"Hermes agent on exe.dev ({name})",
    )
    print(f"  Hub agent: {hub_agent_id}")

    # 2. Save credentials to DB
    save_agent_credentials(name, hub_secret, "", "", telegram_bot_token)
    print("  Credentials saved to DB")

    # 3. Rename Telegram bot to match agent name
    if telegram_bot_token:
        print(f"Renaming Telegram bot to '{name}'...")
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{telegram_bot_token}/setMyName",
                json={"name": name},
                timeout=10.0,
            )
            if resp.json().get("ok"):
                print(f"  Bot renamed to '{name}'")
            else:
                print(f"  Warning: rename failed: {resp.json()}")
        except Exception as e:
            print(f"  Warning: could not rename bot: {e}")

    # 4. Resolve Telegram username → numeric ID (for home_channel)
    telegram_user_id = ""
    owner_name = ""
    if telegram_username and telegram_bot_token:
        print(f"Resolving Telegram user @{telegram_username.lstrip('@')}...")
        try:
            tg_id, tg_name = asyncio.run(
                resolve_telegram_user(f"@{telegram_username.lstrip('@')}")
            )
            telegram_user_id = str(tg_id)
            owner_name = tg_name
            print(f"  Resolved: {owner_name} (id: {telegram_user_id})")
        except Exception as e:
            print(f"  Warning: could not resolve @{telegram_username}: {e}")

    # 5. Build Telegram config blocks (empty if no bot token)
    if telegram_bot_token:
        home_channel_block = ""
        if telegram_user_id:
            home_channel_block = (
                '    home_channel:\n'
                f'      chat_id: "{telegram_user_id}"\n'
                '      name: "Home"\n'
                '      platform: telegram\n'
            )
        telegram_config = (
            '  telegram:\n'
            '    enabled: true\n'
            '    token: "unused"\n'
            + home_channel_block +
            '    extra:\n'
            f'      base_url: "https://tg-{name}.int.exe.xyz"\n'
            f'      base_file_url: "https://tg-{name}.int.exe.xyz"\n'
        )
        soul_telegram = (
            '- **Telegram** — how humans reach you. Anyone can message your bot.\n'
            '  Welcome them — they might be users, collaborators, or people your\n'
            '  owner should know about.\n'
        )
        print(f"  Telegram bot token provided — will configure Telegram platform")
    else:
        telegram_config = ""
        soul_telegram = ""

    # 6. Create VM
    print(f"Creating VM '{name}'...")
    out = run(f"ssh exe.dev new --name={name} --env AGENT_NAME={name}", timeout=30)
    print(f"  {out}")

    # 7. Tag VM (shared integrations: inference, tracing, memory)
    print(f"Tagging VM with '{TAG}', 'langfuse', and 'honcho'...")
    run(f"ssh exe.dev tag {name} {TAG}", timeout=10)
    run(f"ssh exe.dev tag {name} langfuse", timeout=10)
    run(f"ssh exe.dev tag {name} honcho", timeout=10)

    # 8. Create per-agent integrations (zero secrets on VM)
    print(f"Creating per-agent Hub integration...")
    run(
        f"ssh exe.dev integrations add http-proxy"
        f" --name=hub-{name}"
        f" --target=https://admin.slate.ceo"
        f" --header=X-Agent-Secret:{hub_secret}"
        f" --attach=vm:{name}",
        timeout=15,
    )
    if telegram_bot_token:
        print(f"Creating per-agent Telegram integration...")
        run(
            f"ssh exe.dev integrations add http-proxy"
            f" --name=tg-{name}"
            f" --target=https://proxy.slate.ceo"
            f" --header=X-Bot-Token:{telegram_bot_token}"
            f" --attach=vm:{name}",
            timeout=15,
        )

    # 9. Enable email
    print("Enabling inbound email...")
    run(f"ssh exe.dev share receive-email {name} on", timeout=10)

    # 10. Share VM with user and grant SSH + Shelley access
    print(f"Sharing VM with {email}...")
    run(f"ssh exe.dev share add {name} {email}", timeout=10)
    run(f"ssh exe.dev team add {email}", timeout=10, check=False)
    run(f"ssh exe.dev share access allow {name}", timeout=10)

    # 11. Make VM public (products only — Shelley/SSH stay gated)
    print("Making VM public...")
    run(f"ssh exe.dev share set-public {name}", timeout=10)

    # 12. Wait for SSH
    print("Waiting for SSH...")
    if not wait_for_ssh(name):
        raise RuntimeError(f"VM '{name}' not reachable via SSH after 60s")
    print("  SSH ready")

    # 13. Run setup
    print("Running setup (this takes a few minutes)...")
    script = (
        SETUP_SCRIPT
        .replace("{name}", name)
        .replace("{telegram_config}", telegram_config)
        .replace("{soul_telegram}", soul_telegram)
        .replace("{owner_email}", email)
        .replace("{owner_name}", owner_name or email)
    )
    ssh_vm(name, script, timeout=600)

    # 13. Copy cron context scripts to the VM
    print("Copying cron context scripts...")
    scripts_dir = Path(__file__).parent
    for script_file in scripts_dir.glob("*_context.py"):
        run(
            ["scp", "-o", "StrictHostKeyChecking=no",
             str(script_file), f"{name}.exe.xyz:.hermes/scripts/"],
            timeout=30,
        )
        print(f"  {script_file.name}")

    return {
        "name": name,
        "url": f"https://{name}.exe.xyz",
        "shelley": f"https://{name}.shelley.exe.xyz/",
        "ssh": f"ssh {name}.exe.xyz",
        "hub_agent_id": hub_agent_id,
        "telegram_configured": bool(telegram_bot_token),
    }


def destroy_agent(name):
    """Delete a VM, its integrations, and DB record. Returns result dict."""
    from db import delete_agent as db_delete_agent
    print(f"Removing per-agent integrations...")
    run(f"ssh exe.dev integrations remove hub-{name}", timeout=15, check=False)
    run(f"ssh exe.dev integrations remove tg-{name}", timeout=15, check=False)
    print(f"Deleting VM '{name}'...")
    run(f"ssh exe.dev rm {name}", timeout=30)
    print(f"  VM deleted")
    db_delete_agent(name)
    print(f"  DB record removed")
    return {"name": name, "deleted": True}


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <agent-name> <user-email> [telegram-bot-token] [telegram-username]")
        sys.exit(1)

    try:
        result = provision_agent(
            sys.argv[1], sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else "",
            sys.argv[4] if len(sys.argv) > 4 else "",
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"Done! VM: {result['url']}")
    print(f"  Shelley: {result['shelley']}")
    print(f"  SSH:     {result['ssh']}")
    print(f"  Hub:     agent '{result['hub_agent_id']}' on Slate Agent Hub")
    if result["telegram_configured"]:
        print(f"  Telegram: configured (bot token proxied)")


if __name__ == "__main__":
    main()
