# CombinatorAgent → Vela Migration — Completed 2026-04-15

## Overview

Migrate CombinatorAgent from ActiveClaw Docker container (devops) to new provisioner (exe.dev VM) as **Vela**. ActiveClaw → Hermes framework change. Do last.

What matters: two running processes preserved, all credentials handled via integrations, client code updated for DB MCP, customer-development folder ported. Memory starts fresh, SOUL.md is default template, other workspace files are unnecessary.

## Current State

- **Type**: ActiveClaw agent in Docker container
- **Container**: `activeclaw:latest`, up since Apr 10
- **Owner**: Jakub (`jakub@slate.ceo`, `@oogway_defi`)
- **Telegram bot**: `@slate_agent_15_bot` (token in `server.env`)
- **Hub identity**: agent ID `CombinatorAgent` → renamed to `vela` — **must be preserved**
- **Active Cursor SSH session** — Jakub is currently using this agent

## Running Processes and Scheduled Jobs

**Long-running (2):**
1. **openclaw-gateway** → becomes Hermes gateway (standard provisioning)
2. **disc_scraper.main** — Python Discord scraper (asyncpg for DB, discord.py-self for API). Long-running process, 561MB RSS.

**Scheduled syncs (2):**
3. **coda-sync.py** — Coda meeting notes → DB. systemd timer, every 30min. Uses Coda API + psycopg2.
4. **slack-sync.py** — Slack messages → DB. every 5min. Uses Slack API + psycopg2.

**Not migrated:**
- Node server (`server/index.js`) — email webhooks replaced by exe.dev built-in email (`*@vela.exe.xyz` → `~/Maildir/new/`)
- gitwatch.sh — ActiveClaw-specific auto-commit, not needed on Hermes

## Credentials

| Credential | Used By | Integration Model |
|-----------|---------|-------------------|
| Hub secret | Gateway | Standard hub integration (preserve existing identity) |
| Telegram bot token | Gateway | Standard telegram integration |
| Discord tokens | disc-scraper | Stored in DB (`discord_user_sessions.auth_token`), read via MCP at runtime. No integration needed — `discord.py-self` handles REST + WebSocket auth internally. |
| DB URL (`postgresql://agent:...@rds/daryl`) | disc-scraper, coda-sync, slack-sync | postgres-mcp instance (`agent` user, port 8301 — deployed) |
| Coda API token | coda-sync | HTTP proxy to `coda.io`, inject as Bearer header |
| Slack bot token (`xoxb-...-lPZ5...`) | slack-sync | HTTP proxy to `slack.com`, inject as Bearer header (different token from trapezius) |
| GitHub token (`ghp_...`) | Ad-hoc agent use | Not persisted in workspace — passed conversationally by Jakub. Add integration later if needed. |

Slate/OpenAI API keys replaced by standard litellm integration. Brave search key not needed.

## Migration Steps

### 1. Provision the VM

- Agent name: `Vela` (VM name: `slate-vela` — 4 chars triggers `slate-` prefix)
- Owner email: `jakub@slate.ceo`
- Owner telegram: `@oogway_defi`
- Telegram bot token: (from provisioner DB)
- Telegram config: open, home channel `@oogway_defi`
- Standard SOUL.md template
- Provisioner creates a throwaway Hub identity — replaced in step 2

### 2. Rename CombinatorAgent → vela on Hub

One-time manual data migration. Must use lowercase `vela` to match provisioner naming convention (config.yaml already has `agent_id: "vela"`).

- Pull latest: `cd /opt/spice/prod/hub && git pull`
- **Backup**: `cp -r data data.bak.$(date +%s)`
- Stop Hub: `systemctl --user stop my-hub@prod.service`
- `sed -i 's/CombinatorAgent/vela/g'` across all `data/*.json` files (~10K references, mostly obligations.json)
- Delete the throwaway `"vela"` entry from `agents.json` (created by provisioner in step 1 — has wrong secret, would shadow the renamed entry)
- Start Hub: `systemctl --user start my-hub@prod.service`
- Verify: `curl http://127.0.0.1:8081/agents | jq '.agents[] | select(.agent_id=="vela")'`
- Commit: `git add data && git commit -m "rename CombinatorAgent → vela"`

All history (messages, obligations, trust, artifacts) transfers under the new name. Other agents' references update automatically since they're stored Hub-side. Message content mentioning "CombinatorAgent" also changes — cosmetic, not functional.

### 3. Restore Hub secret on VM

The provisioner created the `hub-slate-vela` integration with a throwaway secret. Replace it with CombinatorAgent's original secret so the renamed Hub identity authenticates:

- `ssh exe.dev integrations remove hub-slate-vela`
- `ssh exe.dev integrations add http-proxy --name=hub-slate-vela --target=https://hub.slate.ceo --header=X-Agent-Secret:<hub-secret> --attach=vm:slate-vela`
- Restart hermes (config.yaml agent_id, ws_url, api_base already correct from provisioner)

### 4. Create per-agent integrations and configure agent awareness

Beyond standard hub + telegram:

- `db-slate-vela` → `https://db.slate.ceo` (injects `X-DB-Auth` with agent auth token)
- `coda-slate-vela` → `https://coda.io` (injects Coda API Bearer token)
- `slack-slate-vela` → `https://slack.com` (injects Slack bot Bearer token)

No Discord integration needed — tokens come from DB via MCP, library handles auth directly.

Configure:
- Add DB MCP server to `config.yaml` under `mcp_servers`
- Set env vars in `~/.hermes/.env`:
  - `DB_URL=https://db-slate-vela.int.exe.xyz/sse`
  - `CODA_API_URL=https://coda-slate-vela.int.exe.xyz/apis/v1`
  - `SLACK_API_URL=https://slack-slate-vela.int.exe.xyz/api`
  - `DISCORD_DEVELOPMENT=false`

### 5. Deploy disc-scraper MCP adapter

MCP adapter already built and tested in staging (`staging/vela/disc-scraper/mcp_pool.py` + modified `utils.py`). Copy from staging to VM.

The scraper uses `asyncpg` via a `DB` class in `utils.py`. More complex than trapezius — uses `fetch`, `fetchrow`, `fetchval`, `execute`, and explicit transactions.

**Adapter interface** (Python MCP client replacing asyncpg pool):
- `fetch(sql, *args)` → list of dicts
- `fetchrow(sql, *args)` → single dict or None
- `fetchval(sql, *args)` → single value
- `execute(sql, *args)` → execute without return
- Same JSON wrapping pattern as trapezius: `json_agg(row_to_json(...))` for SELECTs, client-side param interpolation
- Python MCP SDK: `pip install mcp`, use `sse_client()` + `ClientSession` + `call_tool("execute_sql", ...)`
- Read MCP SSE endpoint URL from `DB_URL` env var (e.g. `https://db-vela.int.exe.xyz/sse`)

**Method breakdown** — 15 methods in `DB` class:
- 12 simple (single query, direct MCP adapter — same as trapezius)
- 2 fake transactions (`add_channel`, `add_guild` — single upsert wrapped in unnecessary transaction, drop the wrapper)
- 1 real transaction (`add_message`) — see below

**`add_message` transaction** — the only hard rewrite. Flow: check-exists → INSERT message RETURNING id → INSERT metadata using that id → INSERT N attachments using that id. Steps depend on the returned `id`. Rewrite as a single CTE-based SQL string:
```sql
WITH new_msg AS (INSERT INTO messages (...) VALUES (...) ON CONFLICT DO NOTHING RETURNING *),
     new_meta AS (INSERT INTO discord_metadata (message_id, ...) SELECT id, ... FROM new_msg ON CONFLICT DO NOTHING),
     new_att AS (INSERT INTO discord_attachments (message_id, ...) SELECT m.id, a.* FROM new_msg m, (VALUES (...), (...)) AS a(...) ON CONFLICT DO NOTHING)
SELECT * FROM new_msg
```
Single `execute_sql` call — atomic, returns results, no PL/pgSQL. Attachment VALUES list built dynamically based on attachment count.

**Sync script rewrites** — `coda-sync.py` and `slack-sync.py` are simpler than disc-scraper. Both use synchronous psycopg2 + urllib.request. Changes:
- DB: replace `psycopg2.connect(creds)` with a sync MCP wrapper (use `asyncio.run()` around the async MCP client, or rewrite as async)
- Coda API: replace hardcoded `coda.io` URL with `CODA_API_URL` env var, strip manual `Authorization` header (integration injects it)
- Slack API: replace hardcoded `slack.com/api` URL with `SLACK_API_URL` env var, strip manual `Authorization` header
- Remove credential file reads (`json.load(open("credentials/..."))`) — all handled by integrations

**Discord tokens** — stored in `discord_user_sessions.auth_token` in the DB. The scraper reads them at runtime via MCP, holds them in memory, and uses them to connect to Discord's WebSocket gateway. Tokens never touch disk (zero-secrets maintained). The agent CAN read these tokens via its own DB MCP access — accepted risk, same as prompt injection risk on open Telegram.

### 6. Copy files to VM

- `repos/disc-scraper/` — Discord scraper (from staging, has MCP adapter)
- `sync/coda-sync.py` — Coda sync script (rewrite to use MCP + integration URL)
- `sync/slack-sync.py` — Slack sync script (rewrite to use MCP + integration URL)
- `customer-development/` — business data (from container)
- `slate-meeting-notes/` — meeting notes data (from container)

### 7. Ensure processes and syncs survive VM restarts

**Long-running (systemd services with `Restart=on-failure` + `systemctl enable`):**
- **Hermes gateway**: already handled by standard `hermes.service` from `setup.sh`
- **disc-scraper**: create `/etc/systemd/system/disc-scraper.service`, `ExecStart=python3 -m disc_scraper.main`, `WorkingDirectory=~/repos/disc-scraper`, `EnvironmentFile=~/.hermes/.env`

**Scheduled syncs (Hermes crons in `jobs.json`):**
- **coda-sync**: every 30min, prompt: `cd ~/sync && python3 coda-sync.py`
- **slack-sync**: every 5min, prompt: `cd ~/sync && python3 slack-sync.py`

### 8. Coordinate with Jakub

- Already coordinated — Jakub is aware of the migration
- He'll reconnect to new VM via exe.dev SSH (team add + share access)

### 9. Stop old container

- `docker stop CombinatorAgent`
- Verify disc-scraper running, Hub connected, coda-sync and slack-sync pass on new VM
- Confirm Jakub can SSH in

## Resolved Questions

- **Discord WebSocket**: disc-scraper uses `discord.py-self` (WebSocket-based). Tokens stored in DB (`discord_user_sessions.auth_token`), read at runtime via MCP, held in memory only. Zero-secrets maintained. Agent can read tokens via MCP SQL — accepted risk.
- **Node server**: email webhook replaced by exe.dev built-in email. Not migrated.
- **Port 80**: no longer relevant (Node server dropped).

## Learnings from Trapezius Migration

Patterns established during the first migration that carry forward:

- **postgres-mcp returns Python repr, not JSON.** The MCP adapter wraps SELECT queries in `json_agg(row_to_json(...))` so PostgreSQL returns JSON directly. The adapter parses the JSON from the `_json` column. Must handle `json_agg` returning null on empty result sets.
- **postgres-mcp doesn't support parameterized queries.** The adapter interpolates `$1, $2` params into raw SQL with client-side escaping (`'` → `''`). Acceptable because values come from our own code, not user input.
- **exe.dev integration targets can't have paths.** Slack target is `https://slack.com`, not `https://slack.com/api`. The `/api` prefix goes in the `SLACK_API_URL` env var on the VM.
- **exe.dev integrations require `--header` or `--bearer`.** Even if the upstream doesn't need auth (postgres-mcp handles its own), you must pass one. Use a real auth header — `db.slate.ceo` validates `X-DB-Auth` at the nginx layer.
- **db.slate.ceo must be auth-protected.** nginx checks `X-DB-Auth` header (secret in `/opt/spice/_secrets/nginx-db-auth.conf`, gitignored). Integration injects this header. Without it, the production DB is publicly queryable.
- **CLI provisioning lacks TG_API_ID/TG_API_HASH.** Telegram user resolution fails when running `provision.py` directly. Must manually fix owner name in SOUL.md and home_channel in config.yaml. Or use the API endpoint (service has the env vars).
- **pnpm not pre-installed on exe.dev VMs.** `sudo npm install -g pnpm` needed before installing project deps.
- **Second postgres-mcp instance needs its own port, service, and auth token.** Port 8301 for `agent` user. Same pattern: systemd service (`my-pgmcp-agent@.service`), env file with DATABASE_URL, nginx auth snippet.

## Prerequisites

- [x] Trapezius migration completed (postgres-mcp infra deployed, patterns proven)
- [x] Second postgres-mcp instance deployed for `agent` DB user (port 8301)
- [x] Python MCP adapter built and tested (`staging/vela/disc-scraper/mcp_pool.py`)
- [x] Jakub coordinated
- [x] Sync scripts rewritten for MCP + integration URLs (`staging/vela/sync/`)
