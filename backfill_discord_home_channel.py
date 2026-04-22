#!/usr/bin/env python3
"""Fleet backfill: stamp DISCORD_HOME_CHANNEL = bot↔owner DM channel.id.

Fixes the owner-DM classification bug (reference_discord_owner_classification_bug.md):
  Bug 1 — agents with no DISCORD_HOME_CHANNEL line at all.
  Bug 2 — agents where DISCORD_HOME_CHANNEL = owner's user_id (a distinct
          snowflake from the DM channel.id that inbound messages carry).

For each selected agent:
  1. Read bot token from agent_service_tokens, owner_discord_user_id from agents.
  2. POST /users/@me/channels to resolve (idempotent) the DM channel.id.
  3. SSH to the VM and sed-idempotent-patch ~/.hermes/.env.
  4. Restart hermes.

Usage:
  backfill_discord_home_channel.py --dry-run            # print intended changes
  backfill_discord_home_channel.py --vm=wait4test \\
        --owner-user-id=1417636184355766305              # canary (wait4test
                                                         # has no DB row for owner)
  backfill_discord_home_channel.py --vm=slate-sal       # single VM
  backfill_discord_home_channel.py --all                # every agent with a
                                                         # non-empty owner_user_id
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import sqlite3
from pathlib import Path

from db import get_service_token, DB_PATH
from discord_admin import DiscordAdminError, open_dm_channel


def _list_agents() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT name, vm_name, owner_discord_username, owner_discord_user_id "
        "FROM agents ORDER BY name"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _ssh_patch_env(vm_name: str, channel_id: str, dry_run: bool) -> None:
    # Idempotent sed: replace DISCORD_HOME_CHANNEL=... if present, otherwise
    # append. Using a heredoc keeps the quoting sane over ssh.
    remote = f"""set -eu
if grep -q '^DISCORD_HOME_CHANNEL=' ~/.hermes/.env 2>/dev/null; then
    sed -i -E 's|^DISCORD_HOME_CHANNEL=.*|DISCORD_HOME_CHANNEL={channel_id}|' ~/.hermes/.env
else
    echo 'DISCORD_HOME_CHANNEL={channel_id}' >> ~/.hermes/.env
fi
grep '^DISCORD_HOME_CHANNEL=' ~/.hermes/.env
sudo systemctl restart hermes
echo restarted
"""
    if dry_run:
        print(f"  [dry-run] would ssh {vm_name}.exe.xyz and run:\n{remote}")
        return
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
         f"{vm_name}.exe.xyz", "bash -s"],
        input=remote, text=True, capture_output=True, timeout=180,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ssh patch failed: {r.stderr.strip()}")
    print(f"  {r.stdout.strip()}")


def backfill_one(vm_name: str, owner_user_id: str, dry_run: bool) -> bool:
    print(f"\n== {vm_name} ==")
    if not owner_user_id:
        print("  SKIP: no owner_discord_user_id")
        return False
    bot_token = get_service_token(vm_name, "discord")
    if not bot_token:
        print("  SKIP: no Discord bot token in agent_service_tokens")
        return False
    try:
        channel_id = open_dm_channel(bot_token, owner_user_id)
    except DiscordAdminError as e:
        print(f"  FAIL: {e}")
        return False
    print(f"  resolved DM channel.id={channel_id} (owner_user_id={owner_user_id})")
    try:
        _ssh_patch_env(vm_name, channel_id, dry_run)
    except Exception as e:
        print(f"  FAIL: env patch: {e}")
        return False
    print(f"  OK")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vm", help="Target a single VM (vm_name)")
    ap.add_argument("--owner-user-id",
                    help="Override owner_discord_user_id (required when DB row has none, e.g. wait4test)")
    ap.add_argument("--all", action="store_true",
                    help="Backfill every agent with a non-empty owner_discord_user_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.vm and not args.all:
        ap.error("pass --vm=<name> or --all")

    agents = _list_agents()
    agents_by_vm = {a["vm_name"]: a for a in agents}

    if args.vm:
        if args.vm not in agents_by_vm and not args.owner_user_id:
            print(f"ERROR: vm={args.vm} not in agents table and no --owner-user-id given",
                  file=sys.stderr)
            return 2
        row = agents_by_vm.get(args.vm, {"vm_name": args.vm, "owner_discord_user_id": ""})
        uid = args.owner_user_id or row.get("owner_discord_user_id", "")
        ok = backfill_one(args.vm, uid, args.dry_run)
        return 0 if ok else 1

    # --all: iterate
    failed = []
    skipped = []
    for a in agents:
        vm = a["vm_name"]
        uid = a["owner_discord_user_id"]
        if not vm or not uid:
            skipped.append(vm or a["name"])
            continue
        if not backfill_one(vm, uid, args.dry_run):
            failed.append(vm)
    print(f"\nSummary: {len(agents) - len(skipped) - len(failed)} ok, "
          f"{len(failed)} failed ({failed}), "
          f"{len(skipped)} skipped ({skipped})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
