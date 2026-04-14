"""SQLite agent database."""

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
            telegram_bot_token TEXT,
            status TEXT DEFAULT 'provisioning',
            error TEXT
        )
    """)
    # Add status/error columns if upgrading from old schema
    try:
        con.execute("ALTER TABLE agents ADD COLUMN status TEXT DEFAULT 'ready'")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE agents ADD COLUMN error TEXT")
    except sqlite3.OperationalError:
        pass
    con.commit()
    return con


def save_agent(name: str, hub_secret: str, hub_proxy_token: str,
               tg_proxy_token: str = "", telegram_bot_token: str = ""):
    """Insert or update an agent record."""
    con = _connect()
    con.execute(
        """INSERT INTO agents (name, hub_secret, hub_proxy_token, tg_proxy_token, telegram_bot_token, status)
           VALUES (?, ?, ?, ?, ?, 'provisioning')
           ON CONFLICT(name) DO UPDATE SET
               hub_secret=excluded.hub_secret,
               hub_proxy_token=excluded.hub_proxy_token,
               tg_proxy_token=excluded.tg_proxy_token,
               telegram_bot_token=excluded.telegram_bot_token,
               status='provisioning',
               error=''""",
        (name, hub_secret, hub_proxy_token, tg_proxy_token, telegram_bot_token),
    )
    con.commit()
    con.close()


def set_agent_status(name: str, status: str, error: str = ""):
    """Update an agent's provisioning status."""
    con = _connect()
    con.execute(
        "UPDATE agents SET status = ?, error = ? WHERE name = ?",
        (status, error, name),
    )
    con.commit()
    con.close()


def get_agent(name: str) -> dict | None:
    """Get an agent by name. Returns dict or None."""
    con = _connect()
    row = con.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    con.close()
    return dict(row) if row else None



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


