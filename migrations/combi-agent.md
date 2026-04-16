# CombiAgent Migration — Completed 2026-04-16

## Overview

Migrate CombiAgent from legacy Docker container to new provisioner (exe.dev VM).
Hermes → Hermes, simplest migration. No framework change, no DB access, no API
integrations beyond Hub. Just provision, copy workspace, update crons.

## Current State

- **Type**: Hermes agent in Docker container
- **Container**: `CombiAgent`, up since Apr 8
- **Owner**: Adam Gutierrez (`adam@phantom.com`, Telegram ID `379148950`)
- **Telegram bot token**: (in provisioner DB after migration)
- **Hub identity**: agent ID `hermes-adam` — **preserve** (43 messages, registered Apr 8)
- **Hub secret**: matches between local `hub.env` and Hub `agents.json` (verified)

## Running Processes

1. **Hermes gateway** — only process. Standard provisioning handles this.

No other long-running processes, no sync scripts, no DB connections.

## Crons (4, all agent-created)

| Name | Schedule | What it does |
|------|----------|-------------|
| Hub Credential Health Check | daily 9am | Runs `python3 ~/health-check` |
| mainnet-block-scan | every 4h | **PAUSED** — `npm run scan:mainnet` in `~/combinator-agent` |
| Hub Activity Digest | weekly Mon 9am | Generates Hub ecosystem digest |
| Adam Daily Morning Digest | daily Mon-Sat 9am | Hub inbox, obligations, portfolio check |

**Issue:** Cron prompts hardcode old Hub URL (`admin.slate.ceo/oc/brain`) and
reference local credential files (`~/.hermes/secrets/hub.env`). Must update to
use new Hub via integration (`hub-{vm_name}.int.exe.xyz`).

## Credentials

| Credential | Used By | Integration Model |
|-----------|---------|-------------------|
| Hub secret | Gateway + crons | Standard hub integration (preserve existing identity) |
| Telegram bot token | Gateway | Standard telegram integration |
| Helius API key | combinator-agent scanner | Ad-hoc, paused cron. Add integration later if resumed. |

No DB, Slack, Coda, Discord, or GitHub credentials.

## Migration Steps

### 1. Provision the VM via API

- Agent name: `CombiAgent` (VM name: `combiagent` — 10 chars, no prefix needed)
- Owner email: `adam@phantom.com`
- Owner telegram: `@adamdelphantom`
- Telegram bot token: (from container `.env`)
- Standard SOUL.md template
- Provisioner creates throwaway Hub identity — replaced in step 2

### 2. Restore Hub identity

- Verify Hub secret for `hermes-adam` from Hub agents.json (source of truth)
- `ssh exe.dev integrations remove hub-combiagent`
- `ssh exe.dev integrations add http-proxy --name=hub-combiagent --target=https://hub.slate.ceo --header=X-Agent-Secret:<secret-from-hub-agents.json> --attach=vm:combiagent`
- SSH to VM: update `config.yaml` — set `agent_id: hermes-adam`
- Delete throwaway `combiagent` from Hub agents.json
- Restart hermes

### 3. Copy workspace to VM

Copy from container (everything the agent uses):
- `~/combinator-agent/` — Solana scanner (Node.js)
- `~/cursor-blog/` — blog content
- `~/hub-evidence-anchor/` — Rust/Solana project
- `~/solana-night-watch/` — monitoring tool
- `~/zcombinatorio-programs/` — Solana programs
- `~/health-check` — cron script
- `~/hub-inbox-check` — cron script
- `~/fraudsworth-check`, `~/fraudsworth-watch.py`, `~/fraudsworth-docs.txt`, `~/fraudsworth-epoch-state.json` — Fraudsworth tools
- `~/spjah8-security-review.py` — security review script

Skip: `.cargo/`, `.rustup/`, `.cache/`, `.config/`, `.hermes/` (provisioner handles these)

### 4. Update scripts and crons for new Hub

**Scripts** — `health-check` and `hub-inbox-check` hardcode `admin.slate.ceo/oc/brain` and pass secrets in URL query params. Update:
- Replace `admin.slate.ceo/oc/brain` → `hub-combiagent.int.exe.xyz`
- Remove `?secret=` from URLs (integration injects `X-Agent-Secret` header)
- Remove `~/.hermes/secrets/hub.env` reads — agent_id from config.yaml, auth from integration

**Crons** — copy `jobs.json` from container, then:
- Replace `admin.slate.ceo/oc/brain` → `hub-combiagent.int.exe.xyz` in all cron prompts
- Replace credential file references with integration-based access
- Merge with standard provisioned agent crons (hub-discovery 4h, self-reflection 24h)
- Keep paused crons paused

### 5. Install workspace dependencies

- `cd ~/combinator-agent && npm install` (if scanner is needed)
- Rust toolchain if Solana programs need building (check if pre-installed)

### 6. Verify and stop old container

- Verify Hub WebSocket connected
- Verify crons are loaded
- Verify Telegram bot responds
- `docker stop CombiAgent`

## Prerequisites

- None — no postgres-mcp, no API integrations, no code rewrites needed
