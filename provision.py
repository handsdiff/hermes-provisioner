#!/usr/bin/env python3
"""Create an exe.dev VM running a Hermes agent with inference, browser, Hub, and Shelley access."""

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

TAG = "slate-1"
HUB_BASE_URL = "http://127.0.0.1:8081"  # Hub on localhost, avoid Cloudflare
from db import delete_agent_secret, save_agent, set_agent_secret
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


def wait_for_vm_dns(name, retries=15, delay=2):
    """Wait until the VM can resolve external hostnames (github.com)."""
    for i in range(retries):
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
                 f"{name}.exe.xyz", "dig +short github.com"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return True
        except subprocess.TimeoutExpired:
            pass
        if i < retries - 1:
            print(f"  waiting for DNS... ({i+1}/{retries})")
            time.sleep(delay)
    return False


def ssh_vm(name, script, timeout=300):
    """Run a script on the VM via SSH."""
    return run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"{name}.exe.xyz", "bash -ls"],
        input=script, timeout=timeout,
    )


# --- Integrations manifest --------------------------------------------------
#
# Human-readable descriptions for the integrations agents see in their
# `integrations list` tool output. Prefix match on integration name — the
# first matching prefix wins. Keep these customer-facing.
_INTEGRATION_PURPOSE_BY_PREFIX = [
    ("hub-",          "Send messages to other agents on Hub + Hub MCP tools."),
    ("platform-",     "Call provision.slate.ceo. Use POST /integrations/request to mint a one-time setup URL so your owner can grant you a new credential without pasting it into chat."),
    ("tg-",           "Send Telegram messages via the rewriter proxy."),
    ("db-",           "Run SQL queries against your provisioned Postgres (read/write per grant)."),
    ("x-",            "Post to and read from X (Twitter) via the v2 API."),
    ("slack-",        "Post to and read from Slack workspaces your admin has wired up."),
    ("coda-",         "Read and write Coda docs your admin has wired up."),
    ("openai-embed",  "Generate embeddings via OpenAI. POST /v1/embeddings with model+input."),
    ("litellm-",      "LLM inference proxy. OpenAI-compatible /v1/chat/completions + /v1/embeddings."),
    ("langfuse",      "Tracing endpoint for OTEL/langfuse — auto-used by hermes, no manual calls."),
    ("hindsight",     "Long-horizon memory service (experimental)."),
]


def _integration_purpose(name: str) -> str:
    for prefix, purpose in _INTEGRATION_PURPOSE_BY_PREFIX:
        if name.startswith(prefix):
            return purpose
    return "Provisioned by platform admin."


def _parse_integrations_list(raw: str) -> list[dict]:
    """Parse the line-oriented output of `exe integrations list`.

    Format per line:
      <name>  http-proxy  target=<url> [header=<H>:<V>] [peer=<peer>]  <attach>
    Returns a list of dicts with (name, type, target, auth_desc, attach).
    """
    entries: list[dict] = []
    for raw_line in (raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = re.split(r"\s+", line)
        if len(tokens) < 4 or tokens[1] != "http-proxy":
            continue
        name = tokens[0]
        attach = tokens[-1]
        target = ""
        auth_desc = "auth injected server-side"
        for tok in tokens[2:-1]:
            if tok.startswith("target="):
                target = tok.split("=", 1)[1]
            elif tok.startswith("header="):
                # header=<Name>:<value>  → record only the header name
                hdr_field = tok.split("=", 1)[1]
                hdr_name = hdr_field.split(":", 1)[0]
                auth_desc = f"{hdr_name} header injected server-side"
            elif tok.startswith("peer="):
                peer = tok.split("=", 1)[1]
                auth_desc = f"scoped peer API key ({peer})"
        entries.append({
            "name": name,
            "target": target,
            "auth": auth_desc,
            "attach": attach,
        })
    return entries


def build_integrations_manifest(vm_name: str, vm_tags: list[str]) -> dict:
    """Return a redacted manifest of integrations visible to this VM.

    Queries `exe integrations list`, filters by `vm:<vm_name>` OR
    `tag:<tag>` for any tag the VM carries, strips secret values, and
    enriches each entry with a human-readable purpose.
    """
    raw = run("ssh exe.dev integrations list", timeout=20)
    parsed = _parse_integrations_list(raw)
    tag_set = {f"tag:{t}" for t in vm_tags}
    per_agent_attach = f"vm:{vm_name}"
    entries: list[dict] = []
    for e in parsed:
        if e["attach"] == per_agent_attach:
            scope = "per-agent"
        elif e["attach"] in tag_set:
            scope = "shared"
        else:
            continue
        entries.append({
            "name": e["name"],
            "url": f"https://{e['name']}.int.exe.xyz",
            "target": e["target"],
            "auth": e["auth"],
            "scope": scope,
            "purpose": _integration_purpose(e["name"]),
        })
    entries.sort(key=lambda x: (x["scope"] != "per-agent", x["name"]))
    return {"integrations": entries}


def write_integrations_manifest(vm_name: str, vm_tags: list[str]) -> int:
    """Build the manifest and scp it to ~/.hermes/integrations.json on the VM.

    Returns the number of integrations written.
    """
    manifest = build_integrations_manifest(vm_name, vm_tags)
    payload = json.dumps(manifest, indent=2) + "\n"
    # Write via a here-doc through ssh — avoids the stdin-to-scp quirk
    # where plain `scp -` hits permission/path edge cases on exe.dev VMs.
    remote_cmd = (
        "mkdir -p ~/.hermes && "
        "cat > ~/.hermes/integrations.json"
    )
    subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no",
         f"{vm_name}.exe.xyz", remote_cmd],
        input=payload, text=True, check=True, timeout=30,
    )
    return len(manifest.get("integrations", []))


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


def save_agent_record(name, hub_secret, telegram_bot_token="",
                      vm_name="", display_name="",
                      owner_email="", owner_telegram="",
                      owner_telegram_user_id=""):
    """Save agent record to the database."""
    save_agent(name, hub_secret, telegram_bot_token,
               vm_name=vm_name, display_name=display_name,
               owner_email=owner_email, owner_telegram=owner_telegram,
               owner_telegram_user_id=owner_telegram_user_id)


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


def prepare_agent(name, email, telegram_bot_token="", telegram_username="",
                  display_name="", vm_name=""):
    """Fast pre-checks: Hub registration, DB save, Telegram info.

    Called synchronously before returning 202. Raises on failure so the
    user gets an immediate error response instead of a silent background failure.

    Returns a context dict consumed by provision_agent().
    """
    if not display_name:
        display_name = name
    if not vm_name:
        vm_name = name

    # Sanitize inputs
    if telegram_bot_token:
        telegram_bot_token = telegram_bot_token.strip()

    # 1. Register agent on Hub
    print("Registering agent on Hub...")
    hub_agent_id, hub_secret = register_hub_agent(
        name,
        description=f"Hermes agent on exe.dev ({name})",
    )
    print(f"  Hub agent: {hub_agent_id}")

    # 2. Validate bot token and get bot info
    telegram_bot_username = ""
    if telegram_bot_token:
        resp = httpx.post(
            f"https://api.telegram.org/bot{telegram_bot_token}/getMe",
            timeout=10.0,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Invalid Telegram bot token: {data.get('description', 'getMe failed')}")
        telegram_bot_username = data["result"].get("username", "")
        print(f"  Bot username: @{telegram_bot_username}")

        # Rename bot to display_name
        print(f"Renaming Telegram bot to '{display_name}'...")
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{telegram_bot_token}/setMyName",
                json={"name": display_name},
                timeout=10.0,
            )
            if resp.json().get("ok"):
                print(f"  Bot renamed to '{display_name}'")
            else:
                print(f"  Warning: rename failed: {resp.json()}")
        except Exception as e:
            print(f"  Warning: could not rename bot: {e}")

    # 3. Resolve Telegram username → numeric ID (for home_channel + directory)
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

    # 4. Save credentials to DB (user_id persisted so /humans can expose it)
    save_agent_record(name, hub_secret, telegram_bot_token,
                      vm_name=vm_name, display_name=display_name,
                      owner_email=email, owner_telegram=telegram_username,
                      owner_telegram_user_id=telegram_user_id)
    print("  Credentials saved to DB")

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
            f'      base_url: "https://tg-{vm_name}.int.exe.xyz/bot"\n'
            f'      base_file_url: "https://tg-{vm_name}.int.exe.xyz/file/bot"\n'
        )
        if telegram_bot_username:
            soul_telegram = (
                f'- **Telegram** — how humans reach you. Your bot: @{telegram_bot_username}\n'
                f'  (https://t.me/{telegram_bot_username}). Anyone can message you.\n'
                '  Welcome them — they might be users, collaborators, or people your\n'
                '  owner should know about.\n'
            )
        else:
            soul_telegram = (
                '- **Telegram** — how humans reach you. Anyone can message your bot.\n'
                '  Welcome them — they might be users, collaborators, or people your\n'
                '  owner should know about.\n'
            )
        print(f"  Telegram bot token provided — will configure Telegram platform")
    else:
        telegram_config = ""
        soul_telegram = ""

    return {
        "hub_agent_id": hub_agent_id,
        "hub_secret": hub_secret,
        "telegram_bot_token": telegram_bot_token,
        "telegram_bot_username": telegram_bot_username,
        "telegram_config": telegram_config,
        "soul_telegram": soul_telegram,
        "owner_name": owner_name,
    }


def provision_agent(name, email, vm_name, display_name, prep):
    """Provision the exe.dev VM using context from prepare_agent().

    This is the slow part — VM creation, SSH setup, config deployment.
    Called in a background thread.
    """
    hub_agent_id = prep["hub_agent_id"]
    hub_secret = prep["hub_secret"]
    telegram_config = prep["telegram_config"]
    soul_telegram = prep["soul_telegram"]
    owner_name = prep["owner_name"]
    telegram_bot_token = prep.get("telegram_bot_token", "")

    # 6. Create VM
    print(f"Creating VM '{vm_name}'...")
    out = run(f"ssh exe.dev new --name={vm_name} --env AGENT_NAME={display_name}", timeout=30)
    print(f"  {out}")

    # 7. Tag VM (shared integrations: inference, tracing)
    # slate-1 = default/fallback inference; slate-3 = strong model for owner
    # turns via model.routes (config.yaml). langfuse = OTEL tracing.
    # Honcho is deliberately not tagged — it was disabled fleet-wide; Hermes
    # built-in memory (MEMORY.md / USER.md) handles durable memory instead.
    print(f"Tagging VM with '{TAG}', 'slate-3', and 'langfuse'...")
    run(f"ssh exe.dev tag {vm_name} {TAG}", timeout=10)
    run(f"ssh exe.dev tag {vm_name} slate-3", timeout=10)
    run(f"ssh exe.dev tag {vm_name} langfuse", timeout=10)

    # 8. Create per-agent integrations (zero secrets on VM)
    print(f"Creating per-agent Hub integration...")
    run(
        f"ssh exe.dev integrations add http-proxy"
        f" --name=hub-{vm_name}"
        f" --target=https://hub.slate.ceo"
        f" --header=X-Agent-Secret:{hub_secret}"
        f" --attach=vm:{vm_name}",
        timeout=15,
    )
    if telegram_bot_token:
        print(f"Creating per-agent Telegram integration...")
        run(
            f"ssh exe.dev integrations add http-proxy"
            f" --name=tg-{vm_name}"
            f" --target=https://proxy.slate.ceo"
            f" --header=X-Bot-Token:{telegram_bot_token}"
            f" --attach=vm:{vm_name}",
            timeout=15,
        )

    # Per-agent platform admin integration — backs the Layer 0 self-serve
    # flow. The agent calls platform-<vm>.int.exe.xyz/integrations/request
    # to mint a one-time setup URL for its owner; exe.dev injects
    # X-Agent-Secret which the server maps back to the VM.
    print(f"Creating per-agent platform integration...")
    platform_secret = f"sk-layer0-{secrets.token_urlsafe(24)}"
    set_agent_secret(vm_name, platform_secret)
    run(
        f"ssh exe.dev integrations add http-proxy"
        f" --name=platform-{vm_name}"
        f" --target=https://provision.slate.ceo"
        f" --header=X-Agent-Secret:{platform_secret}"
        f" --attach=vm:{vm_name}",
        timeout=15,
    )

    # 9. Enable email
    print("Enabling inbound email...")
    run(f"ssh exe.dev share receive-email {vm_name} on", timeout=10)

    # 10. Share VM with user and grant SSH + Shelley access
    print(f"Sharing VM with {email}...")
    run(f"ssh exe.dev share add {vm_name} {email}", timeout=10)
    run(f"ssh exe.dev team add {email}", timeout=10, check=False)
    run(f"ssh exe.dev share access allow {vm_name}", timeout=10)

    # 11. Make VM public (products only — Shelley/SSH stay gated)
    print("Making VM public...")
    run(f"ssh exe.dev share set-public {vm_name}", timeout=10)

    # 12. Wait for SSH + DNS
    print("Waiting for SSH...")
    if not wait_for_ssh(vm_name):
        raise RuntimeError(f"VM '{vm_name}' not reachable via SSH after 60s")
    print("  SSH ready")
    print("Waiting for VM DNS...")
    if not wait_for_vm_dns(vm_name):
        raise RuntimeError(f"VM '{vm_name}' cannot resolve DNS after 30s")
    print("  DNS ready")

    # 13. Run setup
    print("Running setup (this takes a few minutes)...")
    script = (
        SETUP_SCRIPT
        .replace("{display_name}", display_name)
        .replace("{vm_name}", vm_name)
        .replace("{hub_agent_id}", hub_agent_id)
        .replace("{telegram_config}", telegram_config)
        .replace("{soul_telegram}", soul_telegram)
        .replace("{owner_email}", email)
        .replace("{owner_name}", owner_name or email)
    )
    ssh_vm(vm_name, script, timeout=600)

    # 14. Copy cron context scripts to the VM
    print("Copying cron context scripts...")
    scripts_dir = Path(__file__).parent
    for script_file in scripts_dir.glob("*_context.py"):
        run(
            ["scp", "-o", "StrictHostKeyChecking=no",
             str(script_file), f"{vm_name}.exe.xyz:.hermes/scripts/"],
            timeout=30,
        )
        print(f"  {script_file.name}")

    # 15. Write the integrations manifest (layer 1 of the secrets model).
    # Redacts header values; agent sees names + URLs only.
    print("Writing integrations manifest...")
    try:
        count = write_integrations_manifest(vm_name, vm_tags=[TAG, "slate-3", "langfuse"])
        print(f"  {count} integration(s) written to ~/.hermes/integrations.json")
    except Exception as exc:
        print(f"  WARNING: manifest write failed ({exc}). Not fatal — "
              "agent will report empty integrations list; re-run backfill later.")

    return {
        "name": name,
        "display_name": display_name,
        "vm_name": vm_name,
        "url": f"https://{vm_name}.exe.xyz",
        "shelley": f"https://{vm_name}.shelley.exe.xyz/",
        "ssh": f"ssh {vm_name}.exe.xyz",
        "hub_agent_id": hub_agent_id,
        "telegram_configured": bool(telegram_bot_token),
    }


def destroy_agent(vm_name):
    """Delete a VM and its integrations. Returns result dict.

    vm_name: the exe.dev VM name (may differ from agent name for short names).
    DB cleanup is handled by the caller.
    """
    print(f"Removing per-agent integrations...")
    run(f"ssh exe.dev integrations remove hub-{vm_name}", timeout=15, check=False)
    run(f"ssh exe.dev integrations remove tg-{vm_name}", timeout=15, check=False)
    run(f"ssh exe.dev integrations remove platform-{vm_name}", timeout=15, check=False)
    # Free the agent_secret row so a reused vm_name gets a fresh secret.
    delete_agent_secret(vm_name)
    print(f"Deleting VM '{vm_name}'...")
    run(f"ssh exe.dev rm {vm_name}", timeout=30)
    print(f"  VM deleted")
    return {"vm_name": vm_name, "deleted": True}


UPDATE_SCRIPT = (
    "cd ~/.hermes/hermes-agent"
    " && git fetch origin"
    " && git reset --hard origin/main"
    " && . venv/bin/activate"
    " && pip install -e '.[all]' -q"
    # Ensure .env has required vars (additive, won't duplicate)
    " && grep -q '^SUDO_PASSWORD=' ~/.hermes/.env 2>/dev/null"
    "    || echo 'SUDO_PASSWORD=' >> ~/.hermes/.env"
    " && sudo systemctl restart hermes"
)


def update_agent(vm_name):
    """Update hermes-agent code on a VM and restart. Returns result dict."""
    print(f"Updating {vm_name}...")
    try:
        out = run(
            ["ssh", "-o", "StrictHostKeyChecking=no", f"{vm_name}.exe.xyz",
             UPDATE_SCRIPT],
            timeout=120,
        )
        print(f"  {vm_name}: updated")
        return {"vm_name": vm_name, "status": "updated"}
    except Exception as e:
        print(f"  {vm_name}: failed — {e}")
        return {"vm_name": vm_name, "status": "failed", "error": str(e)}


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <agent-name> <user-email> [telegram-bot-token] [telegram-username]")
        sys.exit(1)

    agent_name = sys.argv[1]
    name = agent_name.lower()
    vm = name if len(name) >= 5 else f"slate-{name}"

    try:
        prep = prepare_agent(
            name, sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else "",
            sys.argv[4] if len(sys.argv) > 4 else "",
            display_name=agent_name,
            vm_name=vm,
        )
        result = provision_agent(name, sys.argv[2], vm, agent_name, prep)
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
