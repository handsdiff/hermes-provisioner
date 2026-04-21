#!/usr/bin/env python3
"""One-off: import existing per-agent Discord bots into the bot_pool table.

Each existing row is recorded as status='assigned' tied to its current VM —
NOT 'available' — so the provisioner won't hand them to new agents.

Safe to re-run: `add_bot_to_pool` uses ON CONFLICT DO NOTHING on client_id,
then we UPDATE status/assigned_vm explicitly.
"""
from __future__ import annotations

import sys
import time
import httpx

sys.path.insert(0, "/opt/spice/prod/hermes-provisioner")
from db import _connect, get_service_token

# VMs that already have Discord bots wired up via the manual migration.
# Add more here if you discover additional assigned bots.
KNOWN_DISCORD_VMS = ["slate-sal", "wait4test", "slate-vela", "trapezius"]


def identify_bot(token: str) -> dict:
    """Call GET /users/@me to learn the bot's client_id (== user_id)."""
    r = httpx.get(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def main() -> None:
    now = int(time.time())
    for vm in KNOWN_DISCORD_VMS:
        token = get_service_token(vm, "discord")
        if not token:
            print(f"  [skip] {vm}: no discord token in agent_service_tokens")
            continue
        try:
            u = identify_bot(token)
        except Exception as e:
            print(f"  [fail] {vm}: identify call failed: {e}")
            continue
        client_id = str(u["id"])
        username = u.get("username", "")
        con = _connect()
        # Insert or no-op if client_id already present.
        con.execute(
            """INSERT INTO bot_pool (client_id, token, status, assigned_vm,
                                     assigned_at, notes, created_at)
               VALUES (?, ?, 'assigned', ?, ?, ?, ?)
               ON CONFLICT(client_id) DO UPDATE SET
                   status='assigned',
                   assigned_vm=excluded.assigned_vm,
                   assigned_at=COALESCE(bot_pool.assigned_at, excluded.assigned_at),
                   token=excluded.token""",
            (client_id, token, vm, now,
             f"imported from agent_service_tokens (username={username})", now),
        )
        # Also backfill bot_client_id on the agents row (match by vm_name).
        con.execute(
            "UPDATE agents SET bot_client_id=? WHERE vm_name=?",
            (client_id, vm),
        )
        con.commit()
        con.close()
        print(f"  [ok]   {vm}: client_id={client_id} bot_username={username!r}")


if __name__ == "__main__":
    main()
