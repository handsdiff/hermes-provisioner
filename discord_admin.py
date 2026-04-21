"""Discord admin helpers for provisioning.

Uses the platform admin bot (Sal) to resolve Discord usernames to user_ids
against the Slate Discord guild, and the per-agent bot tokens to rename
bots on claim.

Nothing here stores new credentials — it reads existing tokens out of
`agent_service_tokens` and `bot_pool`.
"""
from __future__ import annotations

import logging

import httpx

from db import get_service_token

log = logging.getLogger("discord-admin")

SLATE_GUILD_ID = "1495468808222150796"
_PLATFORM_ADMIN_VM = "slate-sal"
_DISCORD_API = "https://discord.com/api/v10"

# Discord user who gets DM'd when a new agent provision needs the OAuth
# install click (bot-install requires a human with MANAGE_GUILD; see
# reference_discord_bot_install_click.md).
_PLATFORM_ADMIN_USER_ID = "1417636184355766305"  # handsdiff / Niyant


class DiscordAdminError(RuntimeError):
    """Raised when a Discord admin operation fails. Carries a short
    diagnostic the caller can surface to the owner."""


def _admin_bot_token() -> str:
    token = get_service_token(_PLATFORM_ADMIN_VM, "discord")
    if not token:
        raise DiscordAdminError(
            f"no Discord bot token stored for platform admin ({_PLATFORM_ADMIN_VM}); "
            "resolve_discord_user_id cannot run"
        )
    return token


def resolve_discord_user_id(username: str) -> dict | None:
    """Look up a Discord user by username in the Slate guild.

    Returns {'user_id', 'username', 'global_name'} on exact username match
    (case-insensitive), None if no match. Users must be members of the
    Slate Discord guild — this is the onboarding prerequisite.
    """
    u = username.lstrip("@").strip().lower()
    if not u:
        return None
    r = httpx.get(
        f"{_DISCORD_API}/guilds/{SLATE_GUILD_ID}/members/search",
        params={"query": u, "limit": 10},
        headers={"Authorization": f"Bot {_admin_bot_token()}"},
        timeout=15.0,
    )
    if r.status_code != 200:
        raise DiscordAdminError(
            f"Discord members/search failed: HTTP {r.status_code} {r.text[:200]}"
        )
    for member in r.json():
        user = member.get("user") or {}
        if (user.get("username") or "").lower() == u:
            return {
                "user_id": str(user["id"]),
                "username": user.get("username") or "",
                "global_name": user.get("global_name") or "",
            }
    return None


def rename_bot(bot_token: str, new_name: str) -> str:
    """Rename a bot via PATCH /users/@me. Returns the bot's user_id
    (which equals its application client_id for bot accounts).

    Discord's undocumented 2-rename-per-hour limit is the binding
    constraint — surface a clear error if it fires so the provisioner
    can tell the operator to wait.
    """
    r = httpx.patch(
        f"{_DISCORD_API}/users/@me",
        headers={"Authorization": f"Bot {bot_token}"},
        json={"username": new_name},
        timeout=15.0,
    )
    if r.status_code == 400:
        body = r.text
        if "rate" in body.lower() or "too fast" in body.lower():
            raise DiscordAdminError(
                "Discord username rate-limited this bot (2 renames/hour). "
                "Wait an hour and retry, or pull a different bot from the pool."
            )
        raise DiscordAdminError(f"Discord rename rejected: {body[:200]}")
    if r.status_code != 200:
        raise DiscordAdminError(
            f"Discord rename failed: HTTP {r.status_code} {r.text[:200]}"
        )
    return str(r.json()["id"])


def notify_admin_install_pending(
    agent_name: str, vm_name: str, oauth_url: str, dm_url: str
) -> None:
    """DM the platform admin from Sal when a new agent's bot needs the
    OAuth install click. Non-fatal — logs and returns on any failure so
    a provisioning run doesn't get rolled back for a missed ping.
    """
    try:
        token = _admin_bot_token()
        # Open/resolve DM channel with the admin user
        r = httpx.post(
            f"{_DISCORD_API}/users/@me/channels",
            headers={"Authorization": f"Bot {token}"},
            json={"recipient_id": _PLATFORM_ADMIN_USER_ID},
            timeout=15.0,
        )
        if r.status_code not in (200, 201):
            log.warning("admin DM channel open failed: HTTP %s %s",
                        r.status_code, r.text[:200])
            return
        channel_id = r.json()["id"]
        content = (
            f"New agent **{agent_name}** provisioned on `{vm_name}`.\n"
            f"Click to add the bot to the Slate guild: {oauth_url}\n"
            f"Owner DM URL (to pass along): {dm_url}"
        )
        r2 = httpx.post(
            f"{_DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": content},
            timeout=15.0,
        )
        if r2.status_code not in (200, 201):
            log.warning("admin DM send failed: HTTP %s %s",
                        r2.status_code, r2.text[:200])
    except Exception as e:  # noqa: BLE001 — deliberately swallow; non-fatal
        log.warning("notify_admin_install_pending failed: %s", e)
