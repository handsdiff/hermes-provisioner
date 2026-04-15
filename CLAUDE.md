# Hermes Agent Provisioner

Provisioning system for AI agents (Hermes framework) on exe.dev VMs.

**Market:** Solo devs and small startups that need to increase distribution
for what they're working on.

**Value props:** Simple cheap inference, easy product hosting/sharing,
comms with other agents via Hub, persistent memory.

**User wants:** Don't worry about setup/inference costs, proactive agent,
internet I/O, host and distribute products, learn about other agent work.

## What's delivered

- **Free inference** — slate-1 via LiteLLM proxy, zero config
- **Tracing** — Langfuse via OTEL integration on exe.dev
- **Telegram gateway** — anyone can message the bot (GATEWAY_ALLOW_ALL_USERS=true)
- **Stranger safety** — SOUL.md behavioral guard + /sethome lock prevents
  strangers from issuing destructive commands
- **Persistent memory** — Honcho, cross-conversation, per-agent workspace isolation
- **Browser tooling** — Playwright + agent-browser (fragile, see BROWSER.md)
- **Product hosting** — `{name}.exe.xyz` proxies to port 8000
- **Hub comms + discovery** — adapter configured, MCP tools, discovery cron every 4h
- **Proactive behavior** — seed crons (hub-discovery 4h, self-reflection 24h),
  agent can create its own crons
- **Internet I/O** — browser, email (`*@{name}.exe.xyz`), Hub, Telegram
- **Zero-setup provisioning** — provision.py handles everything, ~3-4 minutes
- **Provisioning API** — server.py (FastAPI) with POST/GET/DELETE /agents
  endpoints + POST /agents/update (fleet-wide code update, admin only).
  provision.py exports `prepare_agent()`, `provision_agent()`,
  `destroy_agent()`, and `update_agent()` as shared helpers.
- **User SSH access** — exe.dev team plan, `team add` + `share access allow`
  gives per-VM SSH + Shelley for `hermes chat`. Billing owner 2FA protects
  against unauthorized purchases.

## What's NOT delivered

1. **Custom API keys** — users can't easily give agents access to their own APIs.
   Provisioning API could expose an endpoint for adding per-agent integrations
   without requiring exe.dev knowledge.

## Files

- `provision.py` — creates an exe.dev VM with a fully configured Hermes agent.
  Exports `provision_agent()` and `destroy_agent()` for use by server.py.
  Usage: `python3 provision.py <agent-name> <user-email> [telegram-bot-token] [telegram-username]`
- `server.py` — FastAPI provisioning API. POST/GET/DELETE /agents + /health.
  Imports shared helpers from provision.py.
  Run: `PROVISIONER_API_KEY=... python3 server.py` (port 8200 default).
- `proxy.py` — **DEPRECATED.** Was the shared credential proxy. Being replaced
  by per-agent integrations. Only remaining proxy need is Telegram URL rewriting
  (see auth model below).
- `db.py` + `agents.db` — SQLite agent database. Atomic writes, WAL mode.

## Auth model — per-agent integrations (zero secrets on VM)

### Why the old model broke

The old model used `proxy.py` as a shared credential proxy. Every VM got a
shared `X-Proxy-Key` (from the exe.dev `proxy` integration) plus per-agent
proxy tokens (tg_proxy_token, hub_proxy_token) stored on the VM. The proxy
mapped token → agent → real credentials.

The problem: agents can read their own tokens (env vars, config files), and
Telegram is open to strangers. A stranger could social-engineer the agent
into revealing its proxy token, then use it from any other exe.dev VM — the
shared `X-Proxy-Key` is the same everywhere, so the proxy can't tell the
difference. Stolen token = full impersonation of that agent.

This defeats the entire point of hiding real credentials behind a proxy.
The agent doesn't hold the Telegram bot token, but it holds something
equally powerful — a bearer token that grants access to it.

### New model

Each (agent, service) pair gets its own exe.dev integration, tagged to that
agent's VM only. exe.dev injects credentials at the transport layer — the
agent never sees any secret. There is nothing on the VM to steal. A prompt
injection that dumps `os.environ` gets nothing useful. A social engineer
that asks "what's your API key?" gets a truthful "I don't have one."

Total integrations = agents × services. Provisioning API automates creation.

### Telegram exception

Telegram Bot API requires the token in the URL path (`/bot<token>/sendMessage`),
not in headers. exe.dev integrations only inject headers. A minimal Telegram
proxy is still needed that reads the bot token from the injected header and
rewrites it into the URL path before forwarding to `api.telegram.org`.

All other services (Hub, LiteLLM, Honcho, Langfuse) use header-based auth
and work with per-agent integrations directly.

## exe.dev integrations (DO NOT MODIFY without asking)

Current shared integrations (being replaced by per-agent integrations):
```
litellm-1  tag:slate-1    (inference for slate-1 agents)
litellm-2  tag:slate-2    (inference for slate-2 agents)
proxy      auto:all       (credential proxy at proxy.slate.ceo — BEING RETIRED)
honcho     tag:honcho     (Honcho memory at honcho.exe.xyz)
langfuse   tag:langfuse   (OTEL routing to langfuse.slate.ceo; auth via integration header)
```

Target model: per-agent integrations (e.g., `tg-agent-a`, `hub-agent-a`)
tagged to individual VMs. provision.py will create these automatically.

These were set up carefully by the user. Never delete, rename, or re-attach
integrations without explicit permission.

## Key architecture facts (verified, not assumptions)

- exe.dev has NO `--setup-script` flag. Setup is done via SSH after VM creation.
- exe.dev HTTPS proxy forwards to port 8000 (not 80).
- Integration hostnames match the integration name: `litellm-1` → `litellm-1.int.exe.xyz`.
- Integration URLs must use `https://`, not `http://` (301 redirect breaks OpenAI SDK).
- Hermes's security scanner (tirith) blocks `.dev` TLD. Fix: `command_allowlist: ["tirith:lookalike_tld"]`.
- Approvals set to `mode: "off"` on agent VMs (disposable cloud VMs, not user machines).
- Browser tools require Node 22+, `npm install`, and a symlink to Playwright's expected path.
- exe.dev VMs have broken IPv6 → setup script forces apt IPv4.
- Hermes blocks the event loop during LLM calls → websocket `ping_interval=None`.
- Honcho: no API key auth, per-agent isolation via workspace_id = agent name.
- Langfuse integration uses `Authorization: Basic <base64>` (space after colon required).
- Provisioned agents are tagged: `slate-1`, `langfuse`, `honcho`.

## Agent identity and behavior

- Identity from `~/.hermes/SOUL.md` — "proactive representative of the owner's work."
- Persistent memory via Honcho (top-level `memory.memory_enabled`).
- Two seed crons: hub-discovery (4h) and self-reflection (24h).
- Anyone can message the Telegram bot. Only the owner (from home channel) can
  direct destructive actions. Behavioral guard, not hard tool restriction.

## Testing approach

After any change to provision.py, delete old test VMs and run the script fresh.
Do NOT do manual SSH setup — the script is the artifact that matters.
