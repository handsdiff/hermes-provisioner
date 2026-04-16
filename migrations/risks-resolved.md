# Migration Risk Resolution

Investigated 2026-04-15. These findings apply to the trapezius migration and carry forward to CombiAgent/CombinatorAgent.

---

## 1. postgres-mcp: NOT viable as shared multi-tenant service

**Finding:** [crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp) supports HTTP transport (SSE and streamable-http), BUT the database connection is established once at startup as a global singleton. There is no mechanism for per-request credential injection via headers or MCP metadata. One process = one database.

**Options:**

| Option | Effort | Tradeoff |
|--------|--------|----------|
| **A: Build a lightweight multi-tenant Postgres MCP proxy** | ~150 lines Python, uses psycopg directly | Only need `execute_sql`, `list_schemas`, `list_objects`, `get_object_details`. Skip the index tuning tools. |
| **B: Run one postgres-mcp instance per database** | Zero code changes, operational overhead | Works if # of databases is small and bounded. Each instance gets its own port/integration. |
| **C: Fork postgres-mcp and add per-request credential injection** | ~200-300 lines refactor (server.py + sql_driver.py) | Get all 10 tools for free, but maintain a fork. |

**Recommendation:** Option B — one postgres-mcp instance per DB user. Trapezius connects as `dylanvu`, CombinatorAgent as `agent` (different permissions), so two instances on separate ports. Each agent's exe.dev integration routes to its dedicated instance. Doesn't scale to many DB users (each needs its own process/port), but fine for N=2. Revisit if it becomes a problem.

---

## 2. Capture script rewrites — difficulty assessment

### X/Twitter API: LOW effort

Both projects (`x-team-capture/src/x/client.ts` and `slack-link-capture/src/capture/adapters/x-adapter.ts`) use raw `fetch()` with a hardcoded `X_API_BASE_URL = 'https://api.x.com/2'` and manual `Authorization: Bearer` header.

**Fix:**
- Make `X_API_BASE_URL` read from env var with fallback
- Strip the manual auth header when using the proxy (integration injects it)
- Two files to touch

### Slack API: MODERATE effort

Uses `@slack/web-api` SDK (`WebClient`). The SDK controls both base URL and auth header internally. The code uses 4 Slack API methods: `conversations.list`, `conversations.history`, `conversations.join`, `chat.getPermalink`.

**Options:**
1. Use SDK's `slackApiUrl` option + dummy token — `new WebClient(dummyToken, { slackApiUrl: process.env.SLACK_API_URL })`. Works if the proxy injects the real token and the SDK doesn't break on the dummy.
2. Replace SDK with raw `fetch()` — only 4 methods, and the code already has a clean `SlackApi` interface abstraction. Rewrite `client.ts`, `conversations.ts`, `history.ts`. Higher-level code (`poller.ts`, etc.) unchanged.

### PostgreSQL → DB MCP: MODERATE effort (with adapter pattern)

Both projects use `pg.Pool.query(sql, params)` with ~15 distinct query callsites. Both expose a `Queryable` type: `Pick<Pool, 'query'>`.

**Fix:** Create an MCP-backed implementation of `Queryable` that translates `query(sql, params)` into MCP tool calls (`execute_sql`) and returns the same `{ rows }` shape. Single adapter module — repositories don't change.

**Caveat:** The migration runner in slack-link-capture uses transactions (`BEGIN`/`COMMIT`/`ROLLBACK` via `pool.connect()`). MCP tools are stateless, so transactions can't be proxied. Options:
- Run migrations separately with a direct connection (one-time setup, not ongoing)
- Or skip the MCP adapter for migration runner only

All queries are simple parameterized INSERTs, UPDATEs, SELECTs. No CTEs, JOINs, subqueries, or stored procedures.

---

## 3. Cron persistence: CONFIRMED — fully persists across restarts

**Finding:** Hermes cron jobs fully persist across VM/service restarts. No in-memory-only state.

**How it works:**
- All job state stored in `~/.hermes/cron/jobs.json` (schedule, enabled, last_run_at, next_run_at, repeat count)
- Gateway spawns a cron ticker thread on startup (ticks every 60s)
- Each tick calls `load_jobs()` which reads `jobs.json` from disk
- `compute_next_run()` recalculates using stored schedule + last_run_at + current time
- Missed recurring jobs fast-forward to next future occurrence (no burst on restart)
- File-based lock (`~/.hermes/cron/.tick.lock`) prevents duplicate execution
- Output persisted to `~/.hermes/cron/output/{job_id}/{timestamp}.md`

**Conclusion:** No special handling needed. Seed `jobs.json` during provisioning, and crons survive restarts automatically.
