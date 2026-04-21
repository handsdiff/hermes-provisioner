"""SQLite agent database."""

import sqlite3
import time
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
            owner_telegram_user_id TEXT,
            hub_secret TEXT,
            telegram_bot_token TEXT,
            status TEXT DEFAULT 'provisioning',
            error TEXT
        )
    """)
    # Migrate: add new columns if upgrading from old schema
    for col in ["vm_name", "display_name", "owner_email", "owner_telegram",
                "owner_telegram_user_id", "status", "error"]:
        try:
            con.execute(f'ALTER TABLE agents ADD COLUMN {col} TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
    # Layer 0 self-serve integration flow (2026-04-18).
    # agent_secrets: maps the X-Agent-Secret header value the platform-<vm>
    # integration injects → the vm_name that owns it. Used to authenticate
    # agent-originated credential requests.
    con.execute("""
        CREATE TABLE IF NOT EXISTS agent_secrets (
            secret TEXT PRIMARY KEY,
            vm_name TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL
        )
    """)
    # credential_requests: one-time tokens the agent hands to its owner via
    # chat. Owner clicks the setup URL, pastes the key into a form, submit
    # calls `exe.dev integrations add`.
    con.execute("""
        CREATE TABLE IF NOT EXISTS credential_requests (
            token TEXT PRIMARY KEY,
            vm_name TEXT NOT NULL,
            service_name TEXT NOT NULL,
            target_url TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER
        )
    """)
    # Optional auth_scheme column on credential_requests — lets the agent
    # say "Bot" instead of the default "Bearer" (Discord, etc).
    try:
        con.execute('ALTER TABLE credential_requests ADD COLUMN auth_scheme TEXT DEFAULT "Bearer"')
    except sqlite3.OperationalError:
        pass
    # agent_service_tokens: the raw credential kept server-side for services
    # whose auth can't be injected at the HTTP layer (e.g. Discord gateway
    # IDENTIFY frame). Used by dg-proxy to rewrite outbound frames.
    con.execute("""
        CREATE TABLE IF NOT EXISTS agent_service_tokens (
            vm_name TEXT NOT NULL,
            service_name TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (vm_name, service_name)
        )
    """)
    # gateway_tickets: deprecated. Previously held single-use tokens for the
    # Discord gateway handshake; replaced by X-Agent-Secret header auth on
    # the per-VM `dg-<vm>.int.exe.xyz` integration. Table left in place in
    # existing DBs (empty, unused) to avoid a forced migration.
    #
    # bot_pool: pre-created Discord bot applications the provisioner pulls
    # from when a new agent signs up. Each row holds the application's
    # client_id (also the bot's user_id for bot accounts) and the token.
    # Status: 'available' | 'assigned' | 'retired'. Populated manually by
    # the platform admin via `claim_available_bot`/related helpers after
    # creating bots in the Discord Developer Portal.
    con.execute("""
        CREATE TABLE IF NOT EXISTS bot_pool (
            client_id TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            assigned_vm TEXT,
            assigned_at INTEGER,
            notes TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    # Owner Discord identity columns on the agents table. Added after
    # switching from Telegram-first to Discord-first provisioning.
    for col in ("owner_discord_username", "owner_discord_user_id", "bot_client_id"):
        try:
            con.execute(f'ALTER TABLE agents ADD COLUMN {col} TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
    con.commit()
    return con


def save_agent(name: str, hub_secret: str, telegram_bot_token: str = "",
               vm_name: str = "", display_name: str = "",
               owner_email: str = "", owner_telegram: str = "",
               owner_telegram_user_id: str = "",
               owner_discord_username: str = "",
               owner_discord_user_id: str = "",
               bot_client_id: str = ""):
    """Insert or update an agent record."""
    con = _connect()
    con.execute(
        """INSERT INTO agents (name, vm_name, display_name, owner_email,
               owner_telegram, owner_telegram_user_id,
               owner_discord_username, owner_discord_user_id, bot_client_id,
               hub_secret, telegram_bot_token, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisioning')
           ON CONFLICT(name) DO UPDATE SET
               vm_name=excluded.vm_name,
               display_name=excluded.display_name,
               owner_email=excluded.owner_email,
               owner_telegram=excluded.owner_telegram,
               owner_telegram_user_id=excluded.owner_telegram_user_id,
               owner_discord_username=excluded.owner_discord_username,
               owner_discord_user_id=excluded.owner_discord_user_id,
               bot_client_id=excluded.bot_client_id,
               hub_secret=excluded.hub_secret,
               telegram_bot_token=excluded.telegram_bot_token,
               status='provisioning',
               error=''""",
        (name, vm_name, display_name, owner_email, owner_telegram,
         owner_telegram_user_id, owner_discord_username, owner_discord_user_id,
         bot_client_id, hub_secret, telegram_bot_token),
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


_DIRECTORY_EXCLUDE = {"wait4test"}


def all_humans() -> list[dict]:
    """Return the platform roster of humans reachable through their home agent.

    Discord is the primary reach channel — use `owner_discord_user_id` for
    `<@USER_ID>` @mentions in the Slate Discord #general. `owner_email`
    stays exposed as the stable human identifier. Test/infra VMs are
    excluded.
    """
    con = _connect()
    rows = con.execute(
        """SELECT name, display_name, owner_email,
                  owner_discord_username, owner_discord_user_id
           FROM agents
           WHERE status = 'ready'
           ORDER BY name"""
    ).fetchall()
    con.close()
    return [
        {
            "owner_email": r["owner_email"] or "",
            "owner_discord_username": r["owner_discord_username"] or "",
            "owner_discord_user_id": r["owner_discord_user_id"] or "",
            "hub_agent": r["name"],
            "display_name": r["display_name"] or r["name"],
        }
        for r in rows
        if r["name"] not in _DIRECTORY_EXCLUDE
    ]


# Fields that should never be exposed in public API responses
_PRIVATE_FIELDS = {"hub_secret", "telegram_bot_token", "owner_email", "owner_telegram",
                    "hub_proxy_token", "tg_proxy_token"}


def public_agent_info(agent: dict) -> dict:
    """Strip private fields from an agent record for API responses."""
    return {k: v for k, v in agent.items() if k not in _PRIVATE_FIELDS}


# ---------------------------------------------------------------------------
# Layer 0 — self-serve integration flow
# ---------------------------------------------------------------------------


def set_agent_secret(vm_name: str, secret: str) -> None:
    con = _connect()
    con.execute(
        """INSERT INTO agent_secrets (secret, vm_name, created_at) VALUES (?, ?, ?)
           ON CONFLICT(vm_name) DO UPDATE SET secret=excluded.secret,
                                              created_at=excluded.created_at""",
        (secret, vm_name, int(time.time())),
    )
    con.commit()
    con.close()


def vm_for_agent_secret(secret: str) -> str | None:
    con = _connect()
    row = con.execute(
        "SELECT vm_name FROM agent_secrets WHERE secret = ?", (secret,)
    ).fetchone()
    con.close()
    return row["vm_name"] if row else None


def agent_secret_for_vm(vm_name: str) -> str | None:
    con = _connect()
    row = con.execute(
        "SELECT secret FROM agent_secrets WHERE vm_name = ?", (vm_name,)
    ).fetchone()
    con.close()
    return row["secret"] if row else None


def delete_agent_secret(vm_name: str) -> bool:
    con = _connect()
    cur = con.execute("DELETE FROM agent_secrets WHERE vm_name = ?", (vm_name,))
    con.commit()
    deleted = cur.rowcount > 0
    con.close()
    return deleted


def save_credential_request(
    token: str, vm_name: str, service_name: str, target_url: str,
    description: str, ttl_seconds: int = 900,
) -> int:
    now = int(time.time())
    expires_at = now + ttl_seconds
    con = _connect()
    con.execute(
        """INSERT INTO credential_requests
               (token, vm_name, service_name, target_url, description,
                created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (token, vm_name, service_name, target_url, description, now, expires_at),
    )
    con.commit()
    con.close()
    return expires_at


def get_credential_request(token: str) -> dict | None:
    con = _connect()
    row = con.execute(
        "SELECT * FROM credential_requests WHERE token = ?", (token,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def mark_credential_request_used(token: str) -> bool:
    """Atomically mark a token used. Returns False if already used or missing."""
    con = _connect()
    now = int(time.time())
    cur = con.execute(
        "UPDATE credential_requests SET used_at = ? WHERE token = ? AND used_at IS NULL",
        (now, token),
    )
    con.commit()
    updated = cur.rowcount > 0
    con.close()
    return updated


# ---------------------------------------------------------------------------
# Gateway-proxy services (dg-proxy et al)
# ---------------------------------------------------------------------------


def save_service_token(vm_name: str, service_name: str, token: str) -> None:
    con = _connect()
    con.execute(
        """INSERT INTO agent_service_tokens
               (vm_name, service_name, token, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(vm_name, service_name) DO UPDATE SET
               token=excluded.token,
               created_at=excluded.created_at""",
        (vm_name, service_name, token, int(time.time())),
    )
    con.commit()
    con.close()


def get_service_token(vm_name: str, service_name: str) -> str | None:
    con = _connect()
    row = con.execute(
        "SELECT token FROM agent_service_tokens WHERE vm_name = ? AND service_name = ?",
        (vm_name, service_name),
    ).fetchone()
    con.close()
    return row["token"] if row else None


# ---------------------------------------------------------------------------
# Bot pool — pre-created Discord bots the provisioner assigns to new agents
# ---------------------------------------------------------------------------


def add_bot_to_pool(client_id: str, token: str, notes: str = "") -> None:
    """Seed a new row in bot_pool. Call this after manually creating a bot
    in the Discord Developer Portal."""
    con = _connect()
    con.execute(
        """INSERT INTO bot_pool (client_id, token, status, notes, created_at)
           VALUES (?, ?, 'available', ?, ?)
           ON CONFLICT(client_id) DO NOTHING""",
        (client_id, token, notes, int(time.time())),
    )
    con.commit()
    con.close()


def claim_available_bot(vm_name: str) -> dict | None:
    """Atomically claim an available bot for this VM. Returns {client_id, token}
    or None if the pool is empty. The caller is expected to rename the bot
    via PATCH /users/@me immediately after claiming."""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT client_id, token FROM bot_pool WHERE status='available' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            con.rollback()
            return None
        con.execute(
            "UPDATE bot_pool SET status='assigned', assigned_vm=?, assigned_at=? "
            "WHERE client_id=? AND status='available'",
            (vm_name, int(time.time()), row["client_id"]),
        )
        con.commit()
        return {"client_id": row["client_id"], "token": row["token"]}
    finally:
        con.close()


def get_bot_for_vm(vm_name: str) -> dict | None:
    """Look up the bot assigned to a VM (client_id + token). Returns None
    if the VM has no assigned bot."""
    con = _connect()
    row = con.execute(
        "SELECT client_id, token FROM bot_pool WHERE assigned_vm=? AND status='assigned'",
        (vm_name,),
    ).fetchone()
    con.close()
    return {"client_id": row["client_id"], "token": row["token"]} if row else None


def retire_bot(client_id: str) -> None:
    """Mark a bot as retired (e.g., on agent destruction). Bots are not
    returned to the pool since rename quota (2/hour) and identity cleanup
    make reuse fragile."""
    con = _connect()
    con.execute(
        "UPDATE bot_pool SET status='retired' WHERE client_id=?", (client_id,)
    )
    con.commit()
    con.close()


def pool_status() -> dict:
    """Return counts by status for dashboards/ops."""
    con = _connect()
    rows = con.execute(
        "SELECT status, COUNT(*) AS n FROM bot_pool GROUP BY status"
    ).fetchall()
    con.close()
    return {r["status"]: r["n"] for r in rows}


