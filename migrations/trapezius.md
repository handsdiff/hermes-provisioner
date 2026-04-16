# Trapezius Migration — Completed 2026-04-15

## Overview

Migrate trapezius from old Hermes Docker container (devops) to new provisioner (exe.dev VM).

What matters: two crons running with necessary creds and necessary code logic. Memory doesn't matter, identity doesn't matter, no other folders matter.

## Current State

- **Type**: Hermes agent in Docker container
- **Container**: `ny/hermes`, up since Apr 12
- **Telegram bot**: `@slate_agent_15_bot` (token in provisioner DB)
- **Owner telegram**: `@shirtlessfounder`
- **Hub identity**: None in old system — will get fresh Hub registration via standard provisioning flow

## Active Cron Jobs

1. **slack-link-capture poll** — every 10 min, runs `npx tsx src/cli.ts poll` in `/home/hermes/slack-link-capture`
2. **x-team-capture poll** — every 30 min, runs `npx tsx src/cli.ts poll` in `/home/hermes/x-team-capture`

Both crons have credentials baked into their prompt text.

## Code to Copy

- `slack-link-capture/` — TypeScript project (git repo, pnpm)
- `x-team-capture/` — TypeScript project (git repo, pnpm)

## Credentials (4 total, all need integrations)

| Credential | Used By | Integration Model |
|-----------|---------|-------------------|
| X bearer token | Both capture projects | HTTP proxy to `api.x.com`, inject as Bearer header |
| Slack bot token (`xoxb-...`) + team ID | slack-link-capture | HTTP proxy to `slack.com/api`, inject as Bearer header |
| RDS database URL (`postgresql://dylanvu:...@database-1.clne0yi0xuxo.us-east-1.rds.amazonaws.com:5432/daryl`) | Both capture projects | DB MCP server (postgres-mcp) |
| AWS RDS CA cert | Both capture projects | Bundle into DB MCP server-side, not needed on VM |

No GitHub credentials.

## Migration Steps

### 1. Deploy postgres-mcp instances

Deploy [crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp) as one instance per DB user, each on its own port. Deploy alongside the Telegram rewriter on the provisioner host.

- postgres-mcp is single-tenant (one connection pool per process), so one instance per DB user
- Run with `--transport=sse` or `--transport=streamable-http` for HTTP access
- Trapezius instance: `dylanvu` credentials, port TBD
- Per-agent exe.dev integration routes to this instance
- Agents get native tool access via `mcp_servers` in `config.yaml` (same pattern as Hub MCP)
- Agent-written scripts can call the same endpoint via HTTP, reading the URL from an env var

Doesn't scale to N DB users (one instance/port each). Fine for now. Revisit if needed.

### 2. Rewrite capture scripts

Three changes per project (see migrations/risks-resolved.md for full analysis):

**X/Twitter API (low effort, 2 files):**
- Make `X_API_BASE_URL` read from env var instead of hardcoded `https://api.x.com/2`
- Strip manual `Authorization` header (integration injects it)
- Files: `x-team-capture/src/x/client.ts`, `slack-link-capture/src/capture/adapters/x-adapter.ts`

**Slack API (moderate effort):**
- Use SDK's `slackApiUrl` option: `new WebClient(dummyToken, { slackApiUrl: process.env.SLACK_API_URL })`
- Or replace `@slack/web-api` with raw fetch (only 4 methods used: `conversations.list`, `.history`, `.join`, `chat.getPermalink`)

**PostgreSQL → DB MCP (moderate effort, adapter pattern):**
- Both projects use `pg.Pool.query()` behind a `Queryable` type abstraction
- Create a single MCP-backed `Queryable` adapter that translates `query(sql, params)` → MCP `execute_sql` tool calls
- Repositories don't change, only the adapter module
- Migration runner (transactions) runs separately with direct connection — one-time setup, not ongoing

### 3. Provision the VM

Call the provisioner to create trapezius on exe.dev with standard Hermes setup:
- Agent name: `trapezius`
- Owner email: `dylan@slate.ceo`
- Telegram bot token: carry over from old system
- Telegram config: open (GATEWAY_ALLOW_ALL_USERS=true), with home channel set to `@shirtlessfounder`
- Standard SOUL.md template

This matches the standard provisioned agent pattern — anyone can message, but only the home channel user has elevated trust per SOUL.md ACL. Same pattern for all migrations.

### 4. Create per-agent integrations and configure agent awareness

Beyond the standard hub + telegram integrations:

- `db-{vm_name}` → DB MCP server (injects RDS credentials as headers)
- `x-{vm_name}` → `https://api.x.com` (injects X bearer token as Authorization header)
- `slack-{vm_name}` → `https://slack.com/api` (injects Slack bot token as Authorization header)

Configure agent to use them:
- Add DB MCP server to `config.yaml` under `mcp_servers` (same pattern as Hub MCP)
- Set env vars on the VM so capture scripts can find integration URLs (e.g. `DATABASE_URL`, `SLACK_API_URL`, `X_API_URL` pointing to `*.int.exe.xyz` endpoints)

### 5. Copy code to VM

SCP the capture project repos to the new VM and install dependencies.

### 6. Recreate cron jobs

Set up the two polling crons in the new provisioner's cron format (`~/.hermes/cron/jobs.json`):
- slack-link-capture poll: every 10 min
- x-team-capture poll: every 30 min

Cron prompts reference env vars for credentials instead of hardcoded values.

**Crons must persist across VM restarts.**

### 7. Stop old container

Once the new VM is verified working:
- `docker stop trapezius`
- Verify crons are running on new VM
- Monitor for a few cycles to confirm

## Resolved Risks

All risks investigated and resolved — see `migrations/risks-resolved.md` for details.

- **postgres-mcp**: Single-tenant, one instance per DB user (N=2). Doesn't scale, fine for now.
- **Slack/X rewrites**: Feasible — X is trivial (raw fetch), Slack moderate (SDK supports custom URL).
- **DB rewrite**: Adapter pattern — MCP-backed `Queryable`, repositories unchanged.
- **Cron persistence**: Confirmed — `jobs.json` on disk, loaded every tick, survives restarts.

## Dependencies

This is the first migration. Work done here that carries forward:
- postgres-mcp deployment pattern → reused by CombinatorAgent (second instance)
- Integration creation patterns → templates for other HTTP integrations
- Capture script rewrite pattern (pg → MCP) → pattern for CombinatorAgent's disc-scraper
