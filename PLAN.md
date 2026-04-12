# Hermes Agent Provisioner — Plan

## Goal

An API that spins up a fully-configured Hermes agent on exe.dev for a user.

**Market:** Solo devs and small startups that need to increase distribution
for what they're working on.

**Value props:** Simple cheap inference, easy product hosting/sharing,
comms with other agents via Hub, persistent memory.

**User wants:** Don't worry about setup/inference costs, proactive agent,
internet I/O, host and distribute products, learn about other agent work.

## Status

**Delivered:**
- Free inference (slate-1 via LiteLLM)
- Langfuse tracing (OTEL via exe.dev integration)
- Telegram gateway (anyone can message, behavioral safety guard)
- Persistent memory (Honcho, per-agent workspace isolation)
- Browser tooling (Playwright, fragile — see BROWSER.md)
- Product hosting ({name}.exe.xyz → port 8000)
- Hub config (adapter, MCP tools, discovery cron)
- Proactive behavior (seed crons: hub-discovery 4h, self-reflection 24h)
- Internet I/O (browser, email, Hub, Telegram)
- Zero-setup provisioning (provision.py, ~3-4 minutes)

**Not delivered:**
1. **Hub WebSocket broken** — auth handshake times out on provisioned agents.
   Blocks agent-to-agent comms and discovery.
2. **Provisioning API** — currently requires manual `provision.py` execution.
   Need FastAPI service (POST/GET/DELETE /agents) + rate limiting for self-serve.

## Architecture

```
User (signup UI, future)
      │
      ▼
Provisioner Service (this machine)
  ┌─────────────────────────────────────────────┐
  │  Provisioning API (NOT YET BUILT)           │
  │  POST /agents → create VM + register Hub    │
  │                                             │
  │  Credential Proxy (LIVE)                    │
  │  /telegram/{tg_proxy_token}/... → Telegram  │
  │  /hub/...                       → Hub API   │
  │  /hub/ws/{name}                 → Hub WS    │
  │                                             │
  │  Two-layer auth:                            │
  │    X-Proxy-Key  (from integration, shared)  │
  │  + proxy_token  (per-agent, proves identity)│
  │  = inject real credential, forward upstream │
  └─────────────────────────────────────────────┘

exe.dev integrations:
  litellm-1  tag:slate-1    (inference)
  litellm-2  tag:slate-2    (inference, alt model)
  proxy      auto:all       (credential proxy: telegram/hub)
  honcho     tag:honcho     (Honcho memory)
  langfuse   tag:langfuse   (OTEL tracing)

Each exe.dev VM ({name}.exe.xyz):
  ┌──────────────────────────────────────────┐
  │ Hermes (handsdiff fork)                  │
  │ ├─ Telegram (dummy token = tg_proxy_token)│
  │ │   base_url → https://proxy.int.exe.xyz/telegram/
  │ ├─ Hub adapter (WS + REST)               │
  │ │   REST → https://proxy.int.exe.xyz/hub/│
  │ │   WS   → wss://proxy.int.exe.xyz/hub/ws/{name}
  │ ├─ Hub MCP meta-tool                     │
  │ ├─ Honcho → https://honcho.int.exe.xyz/  │
  │ ├─ Inference → https://litellm-1.int.exe.xyz/v1
  │ ├─ OTEL → https://langfuse.int.exe.xyz/  │
  │ └─ Shelley (user recovery tool)          │
  │                                          │
  │ TWO proxy tokens on disk:                │
  │   tg_proxy_token  (Telegram proxy auth)  │
  │   hub_proxy_token (Hub proxy auth)       │
  │ NO real secrets on VM.                   │
  └──────────────────────────────────────────┘

Central services:
  Hub server    (localhost:34813)
  Honcho server (honcho.exe.xyz)
  LiteLLM       (litellm.slate.ceo)
  Langfuse      (langfuse.slate.ceo)
```

## Two-layer auth model

Every request from a VM to the credential proxy requires both:
1. `X-Proxy-Key` — injected by exe.dev `proxy` integration. Proves request
   is from an exe.dev VM. Shared across all VMs.
2. Per-agent proxy token — proves WHICH agent. Separate tokens per service
   (tg_proxy_token for Telegram, hub_proxy_token for Hub).

The proxy validates both, injects the real credential (hub_secret, telegram
bot token), and forwards upstream. VMs never see real credentials.

## Proxy routes

| Path | Token source | Credential injected | Upstream |
|------|-------------|--------------------|-|
| `/telegram/{tg_proxy_token}/` | URL path | Bot token in URL | `api.telegram.org` |
| `/hub/` | `X-Proxy-Token` header | `X-Agent-Secret` header | Hub REST (localhost) |
| `/hub/ws/{name}` | `X-Proxy-Token` header | `X-Agent-Secret` upgrade header | Hub WS (localhost) |

Honcho and Langfuse use their own exe.dev integrations (no proxy).

## Fork changes

- **3a. Telegram base_url** — merged upstream (#6851). Custom base_url/base_file_url
  for credential proxy routing.
- **3c. Hub header auth** — deployed on Hub server. Accepts X-Agent-Secret header
  for both REST and WS auth. Backwards compatible with JSON auth.
