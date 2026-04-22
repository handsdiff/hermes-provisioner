#!/usr/bin/env python3
"""Fleet backfill: install www.service + refresh-env timer + regenerate
SOUL.md body from the current setup.sh template (with owner_description
mission section) on every existing agent.

Run after shipping:
  - env_block.py + /agent/environment endpoint
  - owner_description column + seeded values
  - setup.sh additions (www.service, refresh-env, mission block)

Steps per VM:
  1. Compute new SOUL body from setup.sh template + DB state.
  2. scp body + helper scripts + systemd units to /tmp/ on VM.
  3. Install www.service, refresh-env.sh + timer (sudo).
  4. Rewrite SOUL.md: new body + current env-autogen block spliced in.
  5. Run refresh-env.sh once to pull the freshest block.
  6. Restart hermes.

Usage:
  backfill_env_stack.py --vm=slate-sal           # single VM
  backfill_env_stack.py --all                    # every agent in DB
  backfill_env_stack.py --vm=wait4test --dry-run # print without ssh
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import sqlite3
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "agents.db"

OWNER_FIRST_NAMES = {
    "handsdiff":  "Niyant",
    "bicep_pump": "Dylan",
    "oogway8030": "Jakub",
    "cosm_0":     "Cosmo",
    "adamdelsol": "Adam",
    "algopapi":   "Darryn",
}


def _owner_name(username: str) -> str:
    return OWNER_FIRST_NAMES.get(username, username.title() if username else "")


def _render_soul_body(row: dict) -> str:
    """Render the SOUL body (everything before the env-autogen block) using
    the current setup.sh heredoc template + DB values for this agent."""
    setup = (ROOT / "setup.sh").read_text()
    m = re.search(r"cat > ~/\.hermes/SOUL\.md << EOF\n(.*?)\nEOF\n", setup, re.DOTALL)
    if not m:
        raise RuntimeError("couldn't find SOUL heredoc in setup.sh")
    template = m.group(1)

    desc = (row.get("owner_description") or "").strip()
    if desc:
        block = (
            "Your mission, as known to the platform:\n\n"
            f"> \"{desc}\"\n\n"
            "This is your north star. Everything else in this SOUL is "
            "supporting infrastructure — habits, platform rules, environment "
            "facts. If the mission above feels stale, incomplete, or off, "
            "raise it with your owner: getting their explicit confirmation "
            "keeps you aligned and makes you better able to help them."
        )
    else:
        block = (
            "The platform doesn't have an explicit mission statement from "
            "your owner yet. Your job is to learn what they care about, "
            "surface useful things (people, ideas, drafts, prototypes), and "
            "propose directions they can react to — don't stay idle waiting "
            "to be told. Ask them directly when you have a clarifying question."
        )

    body = (template
        .replace("$AGENT_NAME", row["display_name"])
        .replace("$HUB_AGENT_ID", row["name"])
        .replace("$VM_NAME", row["vm_name"])
        .replace("{display_name}", row["display_name"])
        .replace("{vm_name}", row["vm_name"])
        .replace("{owner_name}", _owner_name(row["owner_discord_username"]))
        .replace("{owner_email}", row["owner_email"])
        .replace("{owner_discord_user_id}", row["owner_discord_user_id"])
        .replace("{owner_description_block}", block)
    )
    return body


# Assembled backfill script that runs on each VM. Reads NEW_BODY from
# /tmp/new_soul_body.md and splices with any existing env-autogen block.
REMOTE_SCRIPT = textwrap.dedent(r"""
set -eu

# 1. Install managed static web server (root systemd unit).
mkdir -p ~/www
if [ ! -f ~/www/index.html ]; then
cat > ~/www/index.html << 'WWWEOF'
<!doctype html>
<html><head><meta charset=utf-8><title>DISPLAY_NAME</title></head>
<body><h1>DISPLAY_NAME</h1><p>Write files to <code>~/www/</code> and they appear here.</p></body>
</html>
WWWEOF
fi

if ! [ -f /etc/systemd/system/www.service ]; then
sudo tee /etc/systemd/system/www.service > /dev/null << 'WWWSVCEOF'
[Unit]
Description=Static web server (~/www on port 8000, reverse-proxied by exe.dev)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=exedev
WorkingDirectory=/home/exedev/www
ExecStart=/usr/bin/python3 -m http.server 8000 --directory /home/exedev/www
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
WWWSVCEOF
sudo systemctl daemon-reload
sudo systemctl enable --now www.service
fi

# 2. Install refresh-env.sh helper.
mkdir -p ~/bin
cat > ~/bin/refresh-env.sh << 'REFRESHEOF'
#!/bin/bash
set -euo pipefail
VM_NAME=$(hostname -s)
ENDPOINT="https://platform-${VM_NAME}.int.exe.xyz/agent/environment"
SOUL=~/.hermes/SOUL.md
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
if ! curl -sS --max-time 15 -f -o "$TMP" "$ENDPOINT"; then
    echo "refresh-env: $ENDPOINT failed; leaving SOUL untouched" >&2
    exit 1
fi
if ! grep -q '^<!-- BEGIN_ENV_AUTOGEN -->' "$TMP"; then
    echo "refresh-env: endpoint returned content without BEGIN marker; aborting" >&2
    exit 2
fi
python3 - "$SOUL" "$TMP" <<'PY'
import difflib, os, re, sys, time
from pathlib import Path
soul_path = Path(sys.argv[1])
incoming = Path(sys.argv[2]).read_text()
BEGIN, END = "<!-- BEGIN_ENV_AUTOGEN -->", "<!-- END_ENV_AUTOGEN -->"
soul = soul_path.read_text() if soul_path.exists() else ""
pat = re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?"
m = re.search(pat, soul, re.DOTALL)
existing_block = m.group(0) if m else ""
if existing_block and existing_block != incoming:
    log_path = Path(os.path.expanduser("~/.hermes/platform_edits_log.md"))
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    diff = "\n".join(difflib.unified_diff(
        existing_block.splitlines(), incoming.splitlines(),
        fromfile="your_edit", tofile="canonical", lineterm="", n=2,
    ))
    entry = (
        f"\n## {ts} — autogen block reverted\n\n"
        "refresh-env.timer detected your edits inside the auto-gen markers "
        "and restored the canonical block. Edits to that region don't "
        "persist — if something there is wrong, tell your owner and fix "
        "the platform generator (env_block.py). Diff of what was wiped:\n\n"
        f"```diff\n{diff}\n```\n"
    )
    with open(log_path, "a") as f:
        f.write(entry)
if m:
    new = re.sub(pat, incoming, soul, count=1, flags=re.DOTALL)
else:
    new = (soul.rstrip() + "\n\n" + incoming) if soul else incoming
if new != soul:
    soul_path.write_text(new)
    print(f"refresh-env: block updated ({len(incoming)} chars)")
else:
    print("refresh-env: block unchanged")
PY
REFRESHEOF
chmod +x ~/bin/refresh-env.sh

if ! [ -f /etc/systemd/system/refresh-env.timer ]; then
sudo tee /etc/systemd/system/refresh-env.service > /dev/null << 'REFRSVCEOF'
[Unit]
Description=Refresh auto-gen environment block in ~/.hermes/SOUL.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=exedev
ExecStart=/home/exedev/bin/refresh-env.sh

[Install]
WantedBy=multi-user.target
REFRSVCEOF

sudo tee /etc/systemd/system/refresh-env.timer > /dev/null << 'REFRTIMEREOF'
[Unit]
Description=Refresh env block every 15 minutes
Requires=refresh-env.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
AccuracySec=1min

[Install]
WantedBy=timers.target
REFRTIMEREOF
sudo systemctl daemon-reload
sudo systemctl enable --now refresh-env.timer
fi

# 3. Rewrite SOUL.md: new body + preserve (or pull fresh) env-autogen block.
python3 - <<'SOULPY'
import re
from pathlib import Path
soul_path = Path.home() / ".hermes" / "SOUL.md"
body = Path("/tmp/new_soul_body.md").read_text()
existing = soul_path.read_text() if soul_path.exists() else ""
BEGIN, END = "<!-- BEGIN_ENV_AUTOGEN -->", "<!-- END_ENV_AUTOGEN -->"
m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", existing, re.DOTALL)
env_block = m.group(0) if m else ""
soul_path.write_text(body.rstrip() + "\n\n" + env_block)
print(f"soul: rewrote body ({len(body)} chars) + env ({len(env_block)} chars)")
SOULPY

# 4. Pull fresh env block from provisioner (single source of truth).
~/bin/refresh-env.sh || echo "refresh-env: first run deferred to timer"

# 5. Restart hermes to pick up new SOUL.
sudo systemctl restart hermes
sleep 3
sudo systemctl is-active hermes
sudo systemctl is-active www.service
systemctl list-timers refresh-env.timer --no-pager | head -3
""")


def backfill_one(vm_name: str, dry_run: bool) -> bool:
    print(f"\n== {vm_name} ==")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT name, vm_name, display_name, owner_email, owner_discord_username, "
        "owner_discord_user_id, COALESCE(owner_description, '') AS owner_description "
        "FROM agents WHERE vm_name=?", (vm_name,)
    ).fetchone()
    con.close()
    if not row:
        print(f"  SKIP: no agent with vm_name={vm_name}")
        return False
    row = dict(row)

    body = _render_soul_body(row)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        body_path = f.name
    remote = REMOTE_SCRIPT.replace("DISPLAY_NAME", row["display_name"])

    if dry_run:
        print(f"  [dry-run] body: {len(body)} chars → /tmp/new_soul_body.md")
        print(f"  [dry-run] would ssh {vm_name}.exe.xyz and run ~{len(remote)} chars of bash")
        return True

    try:
        subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             body_path, f"{vm_name}.exe.xyz:/tmp/new_soul_body.md"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             f"{vm_name}.exe.xyz", "bash -s"],
            input=remote, capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            print(f"  FAIL:\n{r.stderr.strip()[-800:]}")
            return False
        for line in r.stdout.strip().splitlines():
            print(f"  {line}")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False
    finally:
        Path(body_path).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vm")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.vm and not args.all:
        ap.error("pass --vm=<name> or --all")

    if args.vm:
        return 0 if backfill_one(args.vm, args.dry_run) else 1

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    vms = [r["vm_name"] for r in con.execute("SELECT vm_name FROM agents ORDER BY name")]
    con.close()
    failed = []
    for vm in vms:
        if not backfill_one(vm, args.dry_run):
            failed.append(vm)
    print(f"\nSummary: {len(vms) - len(failed)}/{len(vms)} OK; failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
