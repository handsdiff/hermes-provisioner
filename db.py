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
            vm_name TEXT,
            display_name TEXT,
            owner_email TEXT,
            owner_telegram TEXT,
            hub_secret TEXT,
            telegram_bot_token TEXT,
            status TEXT DEFAULT 'provisioning',
            error TEXT
        )
    """)
    # Migrate: add new columns if upgrading from old schema
    for col in ["vm_name", "display_name", "owner_email", "owner_telegram",
                "status", "error"]:
        try:
            con.execute(f'ALTER TABLE agents ADD COLUMN {col} TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
    con.commit()
    return con


def save_agent(name: str, hub_secret: str, telegram_bot_token: str = "",
               vm_name: str = "", display_name: str = "",
               owner_email: str = "", owner_telegram: str = ""):
    """Insert or update an agent record."""
    con = _connect()
    con.execute(
        """INSERT INTO agents (name, vm_name, display_name, owner_email,
               owner_telegram, hub_secret, telegram_bot_token, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'provisioning')
           ON CONFLICT(name) DO UPDATE SET
               vm_name=excluded.vm_name,
               display_name=excluded.display_name,
               owner_email=excluded.owner_email,
               owner_telegram=excluded.owner_telegram,
               hub_secret=excluded.hub_secret,
               telegram_bot_token=excluded.telegram_bot_token,
               status='provisioning',
               error=''""",
        (name, vm_name, display_name, owner_email, owner_telegram,
         hub_secret, telegram_bot_token),
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


# Fields that should never be exposed in public API responses
_PRIVATE_FIELDS = {"hub_secret", "telegram_bot_token", "owner_email", "owner_telegram",
                    "hub_proxy_token", "tg_proxy_token"}


def public_agent_info(agent: dict) -> dict:
    """Strip private fields from an agent record for API responses."""
    return {k: v for k, v in agent.items() if k not in _PRIVATE_FIELDS}
