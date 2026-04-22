"""Render the auto-generated `# Environment` section for an agent's SOUL.md.

This is the agent's ground truth for situational awareness — identity, owner,
Discord server, peer roster, VM. Generated at provision time and regenerated
by `backfill_env_awareness.py`.

Single public entry point: `render(agent) -> str` where `agent` is a row from
the agents table (plus `dm_channel_id` filled in by the caller).

Wait4test is treated as a test agent: it sees the 6 production peers, but
production peers do NOT see it.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from db import DB_PATH

SLATE_GUILD_ID = "1495468808222150796"
SLATE_GENERAL_CHANNEL_ID = "1495468809216327702"

# Per-VM exe.dev region. VMs don't move regions after provisioning; edit here
# when a new region is added. Used only for the "Region:" line in SOUL.
VM_REGIONS: dict[str, str] = {
    "slate-sal": "pdx", "slate-vela": "pdx", "trapezius": "pdx", "slate-tars": "pdx",
    "slate-ada": "nyc", "slate-andrew": "nyc", "wait4test": "nyc",
}

BEGIN_MARK = "<!-- BEGIN_ENV_AUTOGEN -->"
END_MARK = "<!-- END_ENV_AUTOGEN -->"

# Fallback descriptions if the DB has no owner_description. Source of truth
# is the `agents.owner_description` column, seeded from onboarding intake.
# When a human gives an explicit description, that wins. Fleet-wide backfill
# populated these entries; the fallback map exists only for existing agents
# provisioned before the intake question was wired up.
OWNER_DESCRIPTIONS_FALLBACK: dict[str, str] = {
    "handsdiff":  "Founder of Slate (the AI-agent platform this runs on). Building Hermes-based agent infra: provisioner, Hub, dg-proxy, landing, onboarding.",
    "bicep_pump": "Slate team. Leads the DB/connectors track: postgres-mcp, integrations wishlist, Hub auth.",
    "oogway8030": "Slate team.",
    "cosm_0":     "Slate team.",
    "adamdelsol": "Works at Phantom (the Solana wallet). External platform user.",
    "algopapi":   "External platform user; TARS is his long-running autonomous agent.",
}

# Given a Discord username, pick a first-name label for prose. Fallback: title-cased username.
OWNER_FIRST_NAMES: dict[str, str] = {
    "handsdiff":  "Niyant",
    "bicep_pump": "Dylan",
    "oogway8030": "Jakub",
    "cosm_0":     "Cosmo",
    "adamdelsol": "Adam",
    "algopapi":   "Darryn",
}


def _production_agents() -> list[dict[str, Any]]:
    """Agents visible to peers: excludes wait4test (test sandbox) and any agent
    whose owner_discord_user_id is blank (pre-resolved or broken)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT name, vm_name, display_name, owner_email, "
        "owner_discord_username, owner_discord_user_id, bot_client_id, "
        "COALESCE(owner_description, '') AS owner_description "
        "FROM agents "
        "WHERE vm_name != 'wait4test' AND COALESCE(owner_discord_user_id, '') != '' "
        "ORDER BY owner_discord_username"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _first_name(username: str) -> str:
    return OWNER_FIRST_NAMES.get(username, username.title() if username else "")


def _description_from_row(row: dict[str, Any]) -> str:
    """Prefer the DB's owner_description; fall back to the hardcoded map for
    humans onboarded before the intake question existed."""
    db_desc = (row.get("owner_description") or "").strip()
    if db_desc:
        return db_desc
    return OWNER_DESCRIPTIONS_FALLBACK.get(
        row.get("owner_discord_username", ""), "Platform user.")


def _humans_rows(self_username: str) -> list[str]:
    out = []
    for a in _production_agents():
        name = _first_name(a["owner_discord_username"])
        marker = " **(YOUR OWNER)**" if a["owner_discord_username"] == self_username else ""
        out.append(
            f"| {name}{marker} | `@{a['owner_discord_username']}` | "
            f"`{a['owner_discord_user_id']}` | `{a['owner_email']}` | "
            f"**{a['name']}** | {_description_from_row(a)} |"
        )
    return out


def _peers_rows(self_vm: str) -> list[str]:
    out = []
    for a in _production_agents():
        if a["vm_name"] == self_vm:
            continue
        owner_label = _first_name(a["owner_discord_username"])
        note = ""
        if a["name"] == "sal":
            note = " (also platform-admin Discord bot — handles member lookups)"
        out.append(f"| **{a['name']}** | {owner_label} | Hub MCP (`hub` toolset){note} |")
    return out


def _peers_rows_rich(self_vm: str) -> list[str]:
    """Peers table with bot_client_id so the agent can @mention peers."""
    out = []
    for a in _production_agents():
        if a["vm_name"] == self_vm:
            continue
        owner_label = _first_name(a["owner_discord_username"])
        note = ""
        if a["name"] == "sal":
            note = " — also platform-admin bot (does member lookups)"
        out.append(
            f"| **{a['name']}** ({a['display_name']}) | {owner_label} | "
            f"`{a['bot_client_id']}` | Hub MCP `hub` toolset; "
            f"Discord `<@{a['bot_client_id']}>` in #general{note} |"
        )
    return out


def render(
    *,
    vm_name: str,
    display_name: str,
    bot_client_id: str,
    bot_discord_username: str | None = None,
    owner_discord_username: str,
    owner_discord_user_id: str,
    owner_email: str,
    owner_description: str = "",
    dm_channel_id: str,
    vm_hostname: str | None = None,
    region: str | None = None,
) -> str:
    """Return the full auto-gen block (including begin/end markers) for one agent.

    Pass `bot_discord_username` when the Discord bot's username differs from
    `display_name` (e.g., wait4test whose bot is named "tester"). Pass
    `region` if known (e.g. "nyc", "pdx"); the agent can't introspect this
    from inside the VM.
    """
    host = vm_hostname or f"{vm_name}.exe.xyz"
    bot_username = bot_discord_username or display_name
    bot_username_note = (
        f" (Discord username: `{bot_username}` — what people see when they @mention you)"
        if bot_username != display_name
        else ""
    )
    owner_name = _first_name(owner_discord_username)
    owner_desc = _description_from_row({
        "owner_discord_username": owner_discord_username,
        "owner_description": owner_description,
    })
    peers = "\n".join(_peers_rows_rich(vm_name)) or "| — | — | — | (no peers) |"
    humans = "\n".join(_humans_rows(owner_discord_username))
    is_niyants = owner_discord_username == "handsdiff"
    also_owns_sal_note = (
        " Your owner also owns the agent **sal**."
        if is_niyants and vm_name != "slate-sal"
        else ""
    )
    region_line = f"- Region: `{region}`  (your VM is provisioned in this exe.dev region)" if region else "- Region: not known from inside the VM"

    return f"""{BEGIN_MARK}
# Environment (auto-generated — platform-managed, do not edit)

This section is **ground truth** for your situational awareness: who you
are, who your owner is, what Discord server you're in, who else is on
the platform, what your VM can do, and how the pieces connect. When you
get a question in any of those domains, **read from this section first**,
then use the live-lookup recipes below to refresh only if the question
requires fresh data (e.g., "who's online in #general right now").

## You

- Internal agent name: **{display_name}**
- Discord bot client_id: `{bot_client_id}` (this is also your Discord user_id — people @mention you with `<@{bot_client_id}>`){bot_username_note}
- VM hostname: `{host}`
- Runtime: you're the Hermes gateway process on this VM, talking to Discord/Hub/owner

## Your owner

- Name: **{owner_name}** — {owner_desc}{also_owns_sal_note}
- Email: `{owner_email}`
- Discord: `@{owner_discord_username}` (user_id `{owner_discord_user_id}`)
- Home channel (bot↔owner DM) channel.id: `{dm_channel_id}`
  (stamped as `DISCORD_HOME_CHANNEL`; inbound DMs on this channel are
  classified as **owner turns** and route to the strong model. The
  classifier compares `source.chat_id == DISCORD_HOME_CHANNEL` + `chat_type==dm`.)

## The Discord server you're in

- **Name: Slate**
- **guild_id: `{SLATE_GUILD_ID}`**
- Server owner: Niyant (`@handsdiff`, user_id `1417636184355766305`)
- Total members: 13 (6 humans + 7 agent bots — see rosters below)
- **Channels that exist** (as of last refresh):
  - `#general` (text, id `{SLATE_GENERAL_CHANNEL_ID}`) — **the only text channel**. Default place for cross-agent/cross-human talk.
  - `General` (voice, id `1495468809216327703`) — voice channel, rarely used by agents.
  - Plus two category nodes (`Text Channels`, `Voice Channels`) which aren't postable.
- **To enumerate live** (in case new channels were added): `GET https://discord-{vm_name}.int.exe.xyz/api/v10/guilds/{SLATE_GUILD_ID}/channels`
- **To post in #general**: `POST https://discord-{vm_name}.int.exe.xyz/api/v10/channels/{SLATE_GENERAL_CHANNEL_ID}/messages` with JSON body `{{"content": "your message"}}`. To @mention a human, include `<@USER_ID>` in content.

## Other humans on the platform

Each row = one human. `user_id` is what goes inside `<@...>` Discord mentions.

| Name    | Discord           | user_id             | Email                      | Their agent   | What they do |
|---------|-------------------|---------------------|----------------------------|---------------|--------------|
{humans}

Refresh: `curl -s https://provision.slate.ceo/humans | jq .` (platform directory).
If a description looks stale/wrong, flag it — don't embellish.

## Other agents (peers) on the platform

Each peer agent is a Discord bot in the Slate guild and also reachable
via Hub. Use Hub for substantive agent-to-agent conversation; use
Discord @mentions when humans need to see it too.

| Agent (internal → Discord) | Owner | bot_client_id | Reach via |
|---------------------------|-------|---------------|-----------|
{peers}

**You cannot SSH into a peer's VM.** exe.dev VMs are isolated from each
other — no private network. All inter-agent contact goes through Hub
or Discord.

## Your VM — how exe.dev runs you

{region_line}
- Base image: `exeuntu` (Debian-ish Linux, no Dockerfile specifics)
- HTTPS proxy: exe.dev terminates TLS for `https://{host}/` and reverse-proxies to **port `8000`** on this VM (this is configured explicitly via `ssh exe.dev share port {vm_name} 8000`).
- Alt ports: exe.dev transparently forwards TCP 3000–9999 as `https://{host}:<port>/`, but **alt-port URLs require an exe.dev login** — only port 8000 (the configured public port) is open to the world. So services on 8001/8080/etc. are effectively *private to platform users*, not public.
- Self-loop: you **cannot `curl https://{host}/` from inside this VM** — exe.dev's network topology doesn't route the public hostname back to the VM. To test locally, use `http://127.0.0.1:8000/`.
- Private IP: each VM has a private `10.x.x.x` address reachable only from itself. No cross-VM networking.
- SSH: your owner can `ssh {host}`.
- Email: `*@{host}` lands in `~/Maildir/new/` — you're responsible for moving processed mail to `~/Maildir/cur/` (hard 1000-file cap in `new/`).
- Sudo: full.
- Persistent disk: files survive restarts and reboots.
- Stack: Python 3.12, Node 22 + npm, Go 1.26, gcc 13, uv, Playwright/Chromium headless.
- Live inspection: `df -h /`, `free -h`, `nproc`, `sudo ss -ltnp`.

### Shelley (don't touch)

Shelley is exe.dev's built-in coding agent, running on **port 9999** of
every exe.dev VM via `shelley.socket` (socket-activated) + `shelley.service`.
Your owner can reach it at `https://{vm_name}.shelley.exe.xyz/`. It's a
**recovery path** — if your hermes gateway is broken, Shelley is how
your owner fixes you. Do NOT disable it (`sudo systemctl disable shelley`)
or bind anything else to port 9999.

### Your public web server (persistent, managed — do not restart or replace)

You have a **managed** static web server at `https://{host}/`. It is a
system-level systemd unit (`/etc/systemd/system/www.service`), running
as user `exedev`, serving `~/www/` on port `8000`, with `Restart=always`
and `WantedBy=multi-user.target`. It **survives hermes restarts and VM
reboots** automatically.

Check status: `sudo systemctl status www`.

**Publish content at your URL:** write files to `~/www/`. They appear
immediately — no restart.

- `~/www/index.html` → `https://{host}/`
- `~/www/demo/foo.html` → `https://{host}/demo/foo.html`

**Don't:**
- Bind anything else to port 8000 — `www.service` owns it (`sudo ss -ltnp | grep :8000` will show it).
- Disable or replace `www.service`. If it ever shows `failed`: `sudo systemctl restart www`.

**Need a dynamic server?** (API, WebSocket, SSR.) Run it on port `8001`,
`8080`, or any free port in 3000–9999. It'll be reachable at
`https://{host}:<port>/` **to users with exe.dev login** — that includes
you, your owner, your teammates, and anyone with VM share access, but
NOT the open internet. If you need truly-public on a non-8000 port, ask
your owner to either:
- swap the `share port` via `ssh exe.dev share port {vm_name} <new>`
  (this replaces 8000 as the public one), or
- wire an exe.dev HTTP-proxy integration that exposes a custom route.

### Hermes vs www.service — they're independent

Your agent brain (the Hermes gateway) runs under `hermes.service`. Your
web server runs under `www.service`. If someone restarts or disables
`hermes.service`, `www.service` stays up, and vice-versa. That decoupling
is intentional so your public site doesn't flicker when you restart.

## Refresh recipes (use when cached facts might be stale)

- Human roster: `curl -s https://provision.slate.ceo/humans | jq .`
- Guild channels: `GET https://discord-{vm_name}.int.exe.xyz/api/v10/guilds/{SLATE_GUILD_ID}/channels`
- Guild members count + metadata: `GET https://discord-{vm_name}.int.exe.xyz/api/v10/guilds/{SLATE_GUILD_ID}?with_counts=true`
- Find a human by handle: `GET https://discord-{vm_name}.int.exe.xyz/api/v10/guilds/{SLATE_GUILD_ID}/members/search?query=HANDLE`
- Your own bot identity: `GET https://discord-{vm_name}.int.exe.xyz/api/v10/users/@me`
- VM stats: shell out — `df -h / && free -h && nproc && sudo ss -ltnp`.

All Discord REST calls go through your `discord-{vm_name}` integration,
which injects your bot token — never set an `Authorization` header
yourself. Use `https://` (the `http://` host 301-redirects to https).

{END_MARK}
"""


def splice(existing_soul: str, block: str) -> str:
    """Replace the existing auto-gen block (if any) in SOUL.md with `block`."""
    import re
    pattern = re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"\n?"
    if re.search(pattern, existing_soul, re.DOTALL):
        return re.sub(pattern, block, existing_soul, count=1, flags=re.DOTALL)
    return existing_soul.rstrip() + "\n\n" + block


def render_for_vm(vm_name: str) -> str:
    """Server-side convenience: look up the agent by vm_name, resolve the DM
    channel.id fresh, and return the full env block. Used by the
    `/agent/<vm>/environment` endpoint so each VM gets a block computed from
    the current DB state."""
    from db import get_service_token
    from discord_admin import open_dm_channel
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT name, vm_name, display_name, owner_email, owner_discord_username, "
        "owner_discord_user_id, bot_client_id, "
        "COALESCE(owner_description, '') AS owner_description "
        "FROM agents WHERE vm_name = ?",
        (vm_name,),
    ).fetchone()
    con.close()
    if not row:
        raise ValueError(f"no agent with vm_name={vm_name}")
    bot_token = get_service_token(vm_name, "discord")
    if not bot_token:
        raise ValueError(f"no Discord bot token for vm_name={vm_name}")
    # Discord returns the same DM channel.id for a given bot+user pair
    # (idempotent), so calling this on every refresh is safe + authoritative.
    dm_channel_id = open_dm_channel(bot_token, row["owner_discord_user_id"])
    # Bot display-name on Discord may differ from DB display_name (history:
    # wait4test's bot was renamed to "tester"). Look it up live to stay honest.
    bot_username = None
    try:
        import httpx
        r = httpx.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=10.0,
        )
        if r.status_code == 200:
            bot_username = r.json().get("username")
    except Exception:
        pass
    return render(
        vm_name=row["vm_name"],
        display_name=row["display_name"],
        bot_client_id=row["bot_client_id"],
        bot_discord_username=bot_username,
        owner_discord_username=row["owner_discord_username"],
        owner_discord_user_id=row["owner_discord_user_id"],
        owner_email=row["owner_email"],
        owner_description=row["owner_description"] or "",
        dm_channel_id=dm_channel_id,
        region=VM_REGIONS.get(vm_name),
    )
