"""SQLite agent database — replaces agents.json."""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "agents.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            name TEXT PRIMARY KEY,
            hub_secret TEXT,
            hub_proxy_token TEXT,
            tg_proxy_token TEXT,
            telegram_bot_token TEXT
        )
    """)
    con.commit()
    return con


def save_agent(name: str, hub_secret: str, hub_proxy_token: str,
               tg_proxy_token: str = "", telegram_bot_token: str = ""):
    """Insert or update an agent record."""
    con = _connect()
    con.execute(
        """INSERT INTO agents (name, hub_secret, hub_proxy_token, tg_proxy_token, telegram_bot_token)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               hub_secret=excluded.hub_secret,
               hub_proxy_token=excluded.hub_proxy_token,
               tg_proxy_token=excluded.tg_proxy_token,
               telegram_bot_token=excluded.telegram_bot_token""",
        (name, hub_secret, hub_proxy_token, tg_proxy_token, telegram_bot_token),
    )
    con.commit()
    con.close()


def get_agent(name: str) -> dict | None:
    """Get an agent by name. Returns dict or None."""
    con = _connect()
    row = con.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    con.close()
    return dict(row) if row else None


def lookup_by_hub_token(token: str) -> tuple[str | None, dict]:
    """Find agent by hub_proxy_token."""
    con = _connect()
    row = con.execute(
        "SELECT * FROM agents WHERE hub_proxy_token = ?", (token,)
    ).fetchone()
    con.close()
    if row:
        return row["name"], dict(row)
    return None, {}


def lookup_by_tg_token(token: str) -> tuple[str | None, dict]:
    """Find agent by tg_proxy_token."""
    con = _connect()
    row = con.execute(
        "SELECT * FROM agents WHERE tg_proxy_token = ?", (token,)
    ).fetchone()
    con.close()
    if row:
        return row["name"], dict(row)
    return None, {}


def delete_agent(name: str) -> bool:
    """Delete an agent by name. Returns True if deleted, False if not found."""
    con = _connect()
    cursor = con.execute("DELETE FROM agents WHERE name = ?", (name,))
    con.commit()
    deleted = cursor.rowcount > 0
    con.close()
    return deleted


def all_agents() -> dict:
    """Return all agents as {name: record_dict}."""
    con = _connect()
    rows = con.execute("SELECT * FROM agents").fetchall()
    con.close()
    return {row["name"]: dict(row) for row in rows}


def migrate_from_json(json_path: Path):
    """One-time migration from agents.json to SQLite."""
    if not json_path.exists():
        return 0
    with open(json_path) as f:
        agents = json.load(f)
    count = 0
    for name, record in agents.items():
        save_agent(
            name=name,
            hub_secret=record.get("hub_secret", ""),
            hub_proxy_token=record.get("hub_proxy_token", ""),
            tg_proxy_token=record.get("tg_proxy_token", ""),
            telegram_bot_token=record.get("telegram_bot_token", ""),
        )
        count += 1
    return count
