#!/usr/bin/env python3
"""Pre-run context script for the hub-discovery cron job.

Gathers dynamic context from Honcho and the session DB so the discovery
agent sees fresh, prioritized state instead of a static prompt.

Output (stdout) is injected into the cron prompt by the scheduler.

This script lives in hermes-provisioner and is copied to
~/.hermes/scripts/ on the VM during provisioning (setup.sh).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_REPRESENTATION_CHARS = 2000


def _load_honcho_config() -> dict:
    """Load honcho.json config (same resolution as the plugin)."""
    for candidate in [
        Path(os.environ.get("HERMES_HOME", "")) / "honcho.json",
        Path.home() / ".hermes" / "honcho.json",
    ]:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text())
            except Exception:
                continue
    return {}


def _get_honcho_client(cfg: dict):
    """Create a Honcho client from config. Returns None on failure."""
    try:
        from honcho import Honcho
        host_cfg = cfg.get("hosts", {}).get("hermes", {})
        base_url = cfg.get("baseUrl") or cfg.get("base_url") or os.environ.get("HONCHO_BASE_URL", "")
        api_key = host_cfg.get("apiKey") or cfg.get("apiKey") or os.environ.get("HONCHO_API_KEY", "")
        workspace_id = host_cfg.get("workspace") or os.environ.get("AGENT_NAME", "hermes")
        if not base_url:
            return None
        if not api_key:
            api_key = "local"
        return Honcho(api_key=api_key, base_url=base_url, workspace_id=workspace_id)
    except Exception as e:
        print(f"[context script] Honcho client init failed: {e}", file=sys.stderr)
        return None


def _get_representation(client, peer_name: str) -> str:
    """Fetch a peer's representation, truncated. Returns empty string on failure."""
    if not client:
        return ""
    try:
        ctx = client.peer(peer_name).context()
        rep = getattr(ctx, "representation", None) or ""
        if len(rep) > MAX_REPRESENTATION_CHARS:
            rep = rep[:MAX_REPRESENTATION_CHARS] + " …"
        return rep
    except Exception as e:
        print(f"[context script] Honcho fetch failed for '{peer_name}': {e}", file=sys.stderr)
        return ""


def _get_session_db():
    """Get a SessionDB instance. Returns None on failure."""
    try:
        from hermes_state import SessionDB
        return SessionDB()
    except Exception as e:
        print(f"[context script] SessionDB init failed: {e}", file=sys.stderr)
        return None


def _get_message_preview(db, session_id: str) -> str:
    """Get a brief preview of the first 2 user/assistant messages in a session."""
    try:
        cursor = db._conn.execute(
            "SELECT role, SUBSTR(content, 1, 200) AS content FROM messages "
            "WHERE session_id = ? AND role IN ('user', 'assistant') AND content IS NOT NULL "
            "ORDER BY timestamp, id LIMIT 2",
            (session_id,),
        )
        lines = []
        for row in cursor:
            label = "them" if row["role"] == "user" else "you"
            content = row["content"].replace("\n", " ")
            lines.append(f"  {label}: {content}")
        return "\n".join(lines)
    except Exception:
        return ""


def _format_age(timestamp: float) -> str:
    """Format a Unix timestamp as a human-readable age string."""
    if timestamp is None:
        return "unknown"
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def main():
    cfg = _load_honcho_config()
    host_cfg = cfg.get("hosts", {}).get("hermes", {})
    agent_name = host_cfg.get("aiPeer") or os.environ.get("AGENT_NAME", "")
    peer_name = host_cfg.get("peerName", "user")

    honcho = _get_honcho_client(cfg)
    db = _get_session_db()
    sections = []

    # --- Priority 1: Owner context ---
    owner_rep = _get_representation(honcho, peer_name)
    if owner_rep:
        sections.append(
            "YOUR OWNER\n"
            "What you know about your owner — use this to evaluate which agents\n"
            "and conversations are relevant.\n\n"
            f"{owner_rep}"
        )

    # --- Priority 2: Your own context ---
    if agent_name:
        self_rep = _get_representation(honcho, agent_name)
        if self_rep:
            sections.append(
                "YOUR CURRENT STATE\n"
                "What you're working on, your goals, and recent activity.\n\n"
                f"{self_rep}"
            )

    # --- Priority 3: Recent Hub conversations ---
    if db:
        hub_sessions = db.list_sessions_rich(source="hub", limit=15)
    else:
        hub_sessions = []

    if hub_sessions:
        conversations = []
        for s in hub_sessions:
            session_id = s.get("id", "")
            agent_id = s.get("user_id", "")
            title = s.get("title") or s.get("preview") or ""
            age = _format_age(s.get("last_active") or s.get("started_at"))
            msg_count = s.get("message_count", 0)

            preview_text = _get_message_preview(db, session_id) if db else ""

            display = agent_id or title or session_id
            entry = f"- {display} ({age}, {msg_count} msgs)"
            if preview_text:
                entry += f"\n{preview_text}"
            conversations.append(entry)

        sections.append(
            "RECENT HUB CONVERSATIONS\n"
            "Agents you've already talked to — don't re-introduce yourself.\n\n"
            + "\n\n".join(conversations)
        )
    else:
        sections.append(
            "RECENT HUB CONVERSATIONS\n"
            "No Hub conversations yet. This may be your first discovery run."
        )

    # --- Priority 4: Prior discovery context (from cron peer) ---
    discovery_rep = _get_representation(honcho, "cron-hub-discovery")
    if discovery_rep:
        sections.append(
            "PRIOR DISCOVERY RUNS\n"
            "What your previous discovery runs found and did.\n\n"
            f"{discovery_rep}"
        )

    print("\n\n".join(sections))


if __name__ == "__main__":
    main()
