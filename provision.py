#!/usr/bin/env python3
"""Create an exe.dev VM running a Hermes agent with inference, browser, Hub, and Shelley access."""

import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

TAG = "slate-1"
HUB_BASE_URL = "http://127.0.0.1:8081"  # Hub on localhost, avoid Cloudflare
from db import (
    claim_available_bot,
    delete_agent_secret,
    get_bot_for_vm,
    retire_bot,
    save_agent,
    save_service_token,
    set_agent_secret,
)
from discord_admin import (
    DiscordAdminError,
    notify_admin_install_pending,
    open_dm_channel,
    rename_bot,
    resolve_discord_user_id,
    send_dm_message,
)


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


def vm_tags_from_exe(vm_name: str) -> list[str]:
    """Return the current tag set for a VM by parsing `ssh exe.dev ls`.

    Output line shape: `  • <vm>.exe.xyz - running (...) #tag1 #tag2 ...`
    Returns an empty list if the VM is not found.
    """
    raw = run("ssh exe.dev ls", timeout=15)
    prefix = f"• {vm_name}.exe.xyz "
    for line in raw.splitlines():
        if prefix not in line:
            continue
        return [tok.lstrip("#") for tok in line.split() if tok.startswith("#")]
    return []


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
                      owner_telegram_user_id="",
                      owner_discord_username="",
                      owner_discord_user_id="",
                      bot_client_id="",
                      owner_description=""):
    """Save agent record to the database."""
    save_agent(name, hub_secret, telegram_bot_token,
               vm_name=vm_name, display_name=display_name,
               owner_email=owner_email, owner_telegram=owner_telegram,
               owner_telegram_user_id=owner_telegram_user_id,
               owner_discord_username=owner_discord_username,
               owner_discord_user_id=owner_discord_user_id,
               bot_client_id=bot_client_id,
               owner_description=owner_description)


SETUP_SCRIPT = (Path(__file__).parent / "setup.sh").read_text()


def prepare_agent(name, email, discord_username, display_name="", vm_name="",
                  owner_description=""):
    """Fast pre-checks for Discord-first provisioning.

    Resolves the owner's Discord user_id against the Slate guild, claims
    a bot from the pool, renames it, and saves the agent record. All
    synchronous so failures (user not in Slate, empty pool, rename rate
    limit) return immediately to the API caller instead of a silent
    background blowup.

    `owner_description` is the free-text answer to onboarding's "what
    would you love your agent to do?" — stored in the agents table and
    woven into the agent's SOUL.md at provision time to give it direction
    out of the gate.

    Returns a context dict consumed by `provision_agent()`.
    """
    if not display_name:
        display_name = name
    if not vm_name:
        vm_name = name
    if not discord_username:
        raise RuntimeError("discord_username is required")

    # 1. Resolve the owner's Discord username → user_id (requires they've
    # joined the Slate guild — that's the onboarding prerequisite).
    print(f"Resolving Discord username @{discord_username}...")
    resolved = resolve_discord_user_id(discord_username)
    if not resolved:
        raise RuntimeError(
            f"Discord user '{discord_username}' not found in the Slate guild. "
            "They need to join the Slate Discord first, then retry."
        )
    owner_discord_user_id = resolved["user_id"]
    owner_discord_username = resolved["username"]
    owner_name = (resolved.get("global_name") or owner_discord_username).split()[0].title()
    print(f"  Resolved: {owner_discord_username} (id={owner_discord_user_id}, name={owner_name!r})")

    # 2. Claim a bot from the pool and rename it to the agent's display name.
    print("Claiming bot from pool...")
    bot = claim_available_bot(vm_name)
    if not bot:
        raise RuntimeError(
            "Discord bot pool is empty. Platform admin needs to add more "
            "bots via `db.add_bot_to_pool(client_id, token)`."
        )
    client_id = bot["client_id"]
    bot_token = bot["token"]
    print(f"  Claimed client_id={client_id}")
    try:
        renamed_id = rename_bot(bot_token, display_name)
    except DiscordAdminError as e:
        # Bot is still marked assigned in DB — retire it so we don't try
        # to reuse a known-broken bot. Operator can inspect notes to
        # decide whether to un-retire.
        retire_bot(client_id)
        raise RuntimeError(f"Bot rename failed for {display_name}: {e}") from e
    if renamed_id != client_id:
        # For bot accounts client_id == user_id; mismatch would be surprising
        # but non-fatal — keep going and trust the PATCH response.
        print(f"  WARNING: PATCH /users/@me returned id {renamed_id}, expected {client_id}")
    print(f"  Bot renamed to '{display_name}'")

    # 3. Register agent on Hub
    print("Registering agent on Hub...")
    hub_agent_id, hub_secret = register_hub_agent(
        name,
        description=f"Hermes agent on exe.dev ({name})",
    )
    print(f"  Hub agent: {hub_agent_id}")

    # 4. Save agent + real bot token to DB. dg-proxy reads the token when
    # a Discord WebSocket opens from this VM's dg-<vm> integration.
    save_agent_record(
        name, hub_secret, telegram_bot_token="",
        vm_name=vm_name, display_name=display_name,
        owner_email=email,
        owner_discord_username=owner_discord_username,
        owner_discord_user_id=owner_discord_user_id,
        bot_client_id=client_id,
        owner_description=owner_description,
    )
    save_service_token(vm_name, "discord", bot_token)
    print("  Agent record + bot token saved to DB")

    # 5. Resolve the bot↔owner DM channel.id. Discord DM channel IDs are
    # distinct snowflakes from user IDs — this is what _is_owner_source
    # compares against to classify owner turns (route to slate-3, init
    # AIAgent with user_id=None). Non-fatal: a failure here leaves
    # DISCORD_HOME_CHANNEL empty in .env so the classifier short-circuits
    # gracefully; backfill can fix it later.
    print("Opening bot↔owner DM channel...")
    try:
        dm_channel_id = open_dm_channel(bot_token, owner_discord_user_id)
        print(f"  DM channel.id={dm_channel_id}")
    except DiscordAdminError as e:
        print(f"  WARNING: open_dm_channel failed ({e}). "
              "Agent will ship with DISCORD_HOME_CHANNEL empty — "
              "run backfill_discord_home_channel.py later to fix.")
        dm_channel_id = ""

    # Greet the owner from the newly-claimed bot. bot→user works even
    # without a shared guild, so this lands immediately — and the install
    # link is the thing the owner has to click before user→bot DMs work.
    # Non-fatal: a failed send leaves the owner without the nudge but
    # doesn't roll back provisioning.
    if dm_channel_id:
        greeting = (
            f"Hi {owner_name} — I'm **{display_name}**, your new agent. "
            f"I'm spinning up now (a few more minutes before I'm fully online). "
            f"You'll be able to DM me back here once a Slate admin adds me to the server."
        )
        if owner_description:
            greeting += f"\n\nOnce I'm live, I'll pick up where you left off: *{owner_description}*"
        try:
            send_dm_message(bot_token, dm_channel_id, greeting)
            print("  Sent welcome DM to owner")
        except DiscordAdminError as e:
            print(f"  WARNING: welcome DM send failed ({e}). Non-fatal.")

    return {
        "hub_agent_id": hub_agent_id,
        "hub_secret": hub_secret,
        "owner_name": owner_name,
        "owner_discord_username": owner_discord_username,
        "owner_discord_user_id": owner_discord_user_id,
        "owner_discord_dm_channel_id": dm_channel_id,
        "owner_description": owner_description,
        "bot_client_id": client_id,
        "bot_token": bot_token,
    }


def provision_agent(name, email, vm_name, display_name, prep):
    """Provision the exe.dev VM using context from prepare_agent().

    This is the slow part — VM creation, SSH setup, config deployment.
    Called in a background thread.
    """
    hub_agent_id = prep["hub_agent_id"]
    hub_secret = prep["hub_secret"]
    owner_name = prep["owner_name"]
    owner_discord_user_id = prep["owner_discord_user_id"]
    bot_token = prep["bot_token"]

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

    # 8. Create per-agent integrations (zero secrets on VM).
    print(f"Creating per-agent Hub integration...")
    run(
        f"ssh exe.dev integrations add http-proxy"
        f" --name=hub-{vm_name}"
        f" --target=https://hub.slate.ceo"
        f" --header=X-Agent-Secret:{hub_secret}"
        f" --attach=vm:{vm_name}",
        timeout=15,
    )

    # Discord REST — exe.dev injects the bot token server-side.
    print(f"Creating per-agent Discord REST integration...")
    import shlex as _shlex
    _auth_header = _shlex.quote(f"--header=Authorization:Bot {bot_token}")
    run(
        ["ssh", "exe.dev",
         " ".join([
             "integrations", "add", "http-proxy",
             f"--name=discord-{vm_name}",
             "--target=https://discord.com",
             _auth_header,
             f"--attach=vm:{vm_name}",
         ])],
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

    # Discord gateway proxy — the agent opens a WS to dg-<vm>.int.exe.xyz;
    # exe.dev injects X-Agent-Secret on the upgrade request; dg-proxy
    # validates it + stamps the IDENTIFY frame with the real bot token.
    print(f"Creating per-agent Discord gateway integration...")
    run(
        f"ssh exe.dev integrations add http-proxy"
        f" --name=dg-{vm_name}"
        f" --target=https://discord-gateway.slate.ceo"
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

    # 13. Copy dg_patch.py to the VM before running setup. setup.sh places
    # it into the venv's site-packages + writes the .pth so discord.py is
    # routed through dg-proxy from the moment Hermes first starts.
    print("Copying dg_patch.py to VM...")
    run(
        ["scp", "-o", "StrictHostKeyChecking=no",
         str(Path(__file__).parent / "dg_patch.py"),
         f"{vm_name}.exe.xyz:/tmp/dg_patch.py"],
        timeout=30,
    )

    # 14. Run setup
    print("Running setup (this takes a few minutes)...")
    # Turn the raw owner_description into a "mission" paragraph the SOUL
    # template injects verbatim. Phrasing is neutral about source because the
    # description may be (a) user-provided at onboarding or (b) platform-seeded
    # for agents provisioned before the intake question existed.
    desc = (prep.get("owner_description") or "").strip()
    if desc:
        owner_description_block = (
            f"Your mission, as known to the platform:\n\n"
            f"> \"{desc}\"\n\n"
            "This is your north star. Everything else in this SOUL is "
            "supporting infrastructure — habits, platform rules, environment "
            "facts. If the mission above feels stale, incomplete, or off, "
            "raise it with your owner: getting their explicit confirmation "
            "keeps you aligned and makes you better able to help them."
        )
    else:
        owner_description_block = (
            "The platform doesn't have an explicit mission statement from "
            "your owner yet. Your job is to learn what they care about, "
            "surface useful things (people, ideas, drafts, prototypes), and "
            "propose directions they can react to — don't stay idle waiting "
            "to be told. Ask them directly when you have a clarifying question."
        )
    script = (
        SETUP_SCRIPT
        .replace("{display_name}", display_name)
        .replace("{vm_name}", vm_name)
        .replace("{hub_agent_id}", hub_agent_id)
        .replace("{owner_email}", email)
        .replace("{owner_name}", owner_name or email)
        .replace("{owner_discord_user_id}", owner_discord_user_id)
        .replace("{owner_discord_dm_channel_id}", prep.get("owner_discord_dm_channel_id", ""))
        .replace("{owner_description_block}", owner_description_block)
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

    client_id = prep["bot_client_id"]
    oauth_url = f"https://discord.com/oauth2/authorize?client_id={client_id}"
    dm_url = f"https://discord.com/users/{client_id}"

    # DM the platform admin so they can click the OAuth install URL.
    # Non-fatal — see notify_admin_install_pending docstring.
    print("Notifying platform admin via Discord DM...")
    notify_admin_install_pending(display_name, vm_name, oauth_url)

    return {
        "name": name,
        "display_name": display_name,
        "vm_name": vm_name,
        "url": f"https://{vm_name}.exe.xyz",
        "shelley": f"https://{vm_name}.shelley.exe.xyz/",
        "ssh": f"ssh {vm_name}.exe.xyz",
        "hub_agent_id": hub_agent_id,
        "bot_client_id": client_id,
        "dm_url": dm_url,
        "oauth_url": oauth_url,
        "owner_discord_user_id": owner_discord_user_id,
    }


def destroy_agent(vm_name):
    """Delete a VM and its integrations. Returns result dict.

    vm_name: the exe.dev VM name (may differ from agent name for short names).
    DB cleanup is handled by the caller.
    """
    print(f"Removing per-agent integrations...")
    for integ in (
        f"hub-{vm_name}",
        f"tg-{vm_name}",          # legacy — pre-Discord-first agents
        f"platform-{vm_name}",
        f"discord-{vm_name}",     # REST (created post-provision via Layer 0)
        f"dg-{vm_name}",          # gateway (created post-provision via Layer 0)
    ):
        run(f"ssh exe.dev integrations remove {integ}", timeout=15, check=False)
    # Free the agent_secret row so a reused vm_name gets a fresh secret.
    delete_agent_secret(vm_name)
    print(f"Deleting VM '{vm_name}'...")
    run(f"ssh exe.dev rm {vm_name}", timeout=30)
    print(f"  VM deleted")
    return {"vm_name": vm_name, "deleted": True}


# UPDATE_SCRIPT runs on the VM after provision.py scps a fresh dg_patch.py
# to /tmp/dg_patch.py. The script reinstalls hermes, ensures discord.py is
# present, refreshes dg_patch.py in site-packages, and restarts the service.
UPDATE_SCRIPT = (
    "cd ~/.hermes/hermes-agent"
    " && git fetch origin"
    " && git reset --hard origin/main"
    " && . venv/bin/activate"
    " && pip install -e '.[all]' -q"
    " && pip install -q 'discord.py>=2.5'"
    # Refresh dg_patch.py + .pth (idempotent; /tmp/dg_patch.py scp'd by caller)
    " && SP=$(python -c \"import site; print([p for p in site.getsitepackages() if p.endswith('site-packages')][0])\")"
    " && if [ -f /tmp/dg_patch.py ]; then"
    "      cp /tmp/dg_patch.py \"$SP/dg_patch.py\";"
    "      echo 'import dg_patch' > \"$SP/dg_patch.pth\";"
    "      rm -f \"$SP/__pycache__/dg_patch.\"*.pyc;"
    "    fi"
    # Ensure .env has required vars (additive, won't duplicate)
    " && grep -q '^SUDO_PASSWORD=' ~/.hermes/.env 2>/dev/null"
    "    || echo 'SUDO_PASSWORD=' >> ~/.hermes/.env"
    " && sudo systemctl restart hermes"
)


def update_agent(vm_name):
    """Update hermes-agent code on a VM and restart. Returns result dict.

    Also refreshes dg_patch.py on the VM — scp's the current copy from
    the provisioner repo before running the remote update script, so
    fleet updates pick up dg-patch changes automatically.
    """
    print(f"Updating {vm_name}...")
    try:
        # scp the current dg_patch.py first; UPDATE_SCRIPT reads it from /tmp.
        run(
            ["scp", "-o", "StrictHostKeyChecking=no",
             str(Path(__file__).parent / "dg_patch.py"),
             f"{vm_name}.exe.xyz:/tmp/dg_patch.py"],
            timeout=30,
        )
        run(
            ["ssh", "-o", "StrictHostKeyChecking=no", f"{vm_name}.exe.xyz",
             UPDATE_SCRIPT],
            timeout=180,
        )
        print(f"  {vm_name}: updated")
        return {"vm_name": vm_name, "status": "updated"}
    except Exception as e:
        print(f"  {vm_name}: failed — {e}")
        return {"vm_name": vm_name, "status": "failed", "error": str(e)}


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <agent-name> <owner-email> <owner-discord-username>")
        sys.exit(1)

    agent_name = sys.argv[1]
    name = agent_name.lower()
    # Always prefix with `slate-` so VM names are globally unique and don't
    # collide with exe.dev reserved names (e.g. `andrew` was rejected).
    vm = name if name.startswith("slate-") else f"slate-{name}"

    try:
        prep = prepare_agent(
            name, sys.argv[2], sys.argv[3],
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
    print(f"  DM URL:  {result['dm_url']}  (send to owner)")
    print(f"  OAuth:   {result['oauth_url']}  (admin: click to add bot to Slate)")


if __name__ == "__main__":
    main()
