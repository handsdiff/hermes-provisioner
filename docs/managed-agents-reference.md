# Anthropic Managed Agents — architecture, the confused-deputy finding, and what it means for us

**Sources.** Anthropic's *Managed Agents Overview* (platform.claude.com) and hi120ki's 2026-04-13 security writeup identifying a confused-deputy vulnerability in the credential-vault access model. Captured here as our interpretation, not verbatim. Purpose: benchmark against the closest architectural competitor and record why the vulnerability they shipped validates our own credential model.

## The one-line take

**Anthropic built the same shape as our per-agent integration model, but at the API layer instead of the network layer — and the cost of that choice is exactly the vulnerability hi120ki surfaced. Our wedge is identity that's enforced by the network, not by whoever holds the bearer key.**

## Managed Agents in one page

**What it is.** A hosted agent harness. You define an agent (model + system prompt + tools + MCP servers + skills), an environment (container template with packages and network rules), and launch sessions that combine them. The harness runs the loop: tool execution, streaming, event history, prompt caching, compaction, steering/interruption. Beta as of 2026-04-01, gated by a beta header.

**Four concepts.**
- **Agent** — declarative config, reusable across sessions.
- **Environment** — container template.
- **Session** — running instance. Persisted file system, event history fetchable server-side.
- **Credential Vault** — stores OAuth tokens and secrets that MCP servers need to authenticate outbound.

**The brain-and-hands split.** The sandbox container (the "brain" — model + tool executor) never sees raw credentials. A dedicated proxy layer sits between the sandbox and the outside world, holds the vault tokens, and injects them at call time. Prompt injection in the sandbox can't dump secrets because there's nothing to dump — this is the same architectural bet we're making with per-agent exe.dev integrations.

**Supported built-in tools.** Bash, file ops, web search/fetch, MCP. Agents can read/write in the container, run code, hit the web, and call MCP servers configured on the agent.

**Rate limits.** 60/min writes, 600/min reads, per-organization.

**Unified capabilities that matter for positioning.** Long-running execution (minutes to hours), stateful sessions with persistent FS, cloud-side sandbox so the caller supplies nothing infrastructural. Memory + outcomes + multi-agent are flagged as research preview.

## The vulnerability: confused deputy on `vault_ids`

**What hi120ki demonstrated.** When a caller creates a session, they pass `vault_ids` to bind vaults to the session. The API enforces *that the caller holds an API key with access* — it does not enforce *that the vaults belong to the caller*.

**The Alice-Bob scenario, in one sentence.** Alice stores her Notion OAuth token in Vault A. Bob, sharing the same workspace API key or holding any key with workspace access, creates a session and specifies `vault_ids=[A]`. Bob's session now makes authenticated Notion calls as Alice.

**Anthropic's own UI flags this as the model's explicit behavior** — "This credential will be shared across this workspace. Anyone with API key access can use this credential in an agent session." The platform describes it as a shared resource; it does not provide a primitive for enforcing per-user ownership. Their doc explicitly punts: *access control in multi-user environments is something you build on top.*

**Why it's a confused-deputy specifically.** The vault has the authority to act as Alice on Notion. The session creation API is the deputy that invokes that authority on behalf of whoever calls it. The authority check is "does the caller have access to *this API*?" rather than "does the caller own *this authority*?" Classic pattern; has been known since 1988.

**Author's mitigation recipe.** Don't expose the Console/Vault API directly to users. Wrap it behind your own service: authenticate the user (IAP/OAuth), map them to the vaults they're allowed to use (metadata field), and resolve `vault_ids` server-side at session creation. The author shipped a reference implementation — but the point is that mitigation lives *outside* Managed Agents; the platform gives you no primitive for it.

## What this validates in our model

Our per-agent integration architecture pushes identity and authorization down to the network layer, not the API layer. Concretely:

- **Integration scope is VM-scope.** `hub-trapezius` is tagged to the `trapezius` VM. An agent running on any other VM cannot invoke that integration — not because a bearer key check fails, but because the DNS name (`hub-trapezius.int.exe.xyz`) only resolves on the trapezius link-local network.
- **Spoofing requires compromising the exe.dev network substrate**, not just holding a key. A stolen API key to our provisioner would let someone create *new* agents — it would not let them hijack an *existing* agent's credentials, because the credentials are bound to a VM they don't control.
- **There is no `vault_ids` parameter**, because there is no vault identity that a caller nominates at request time. Integration-to-VM binding is created at provision time and enforced by routing. The confused-deputy gap has no analogous surface.

The article Anthropic published on their own doc page makes this trade-off visible: they note that the vault is a workspace-shared resource and that per-user enforcement is the developer's problem. Our target market (solo devs and small startups spinning up public agents) is precisely the population that is least equipped to build that missing enforcement layer. This is the 4th property of our competitive framework (`competitive-landscape.md`) — network identity — and the hi120ki writeup is the clearest public evidence that it's not a theoretical concern.

## What Anthropic got right that we already match (or should steal)

| Primitive | Anthropic | Us |
|---|---|---|
| Hosted runtime with persistent FS | Managed Agents session | exe.dev VM |
| Secrets never in sandbox | Vault proxy | exe.dev integration header injection |
| Declarative agent config | Agent object (model/prompt/tools/MCP/skills) | SOUL.md + skills + config.yaml + per-agent integrations |
| Event history persisted | Server-side event log | Session JSON + Langfuse traces |
| MCP as primary tool extension | First-class | Hub MCP, db MCP — already in |
| Compaction, prompt caching built-in | Harness-level | Hermes gateway handles this (with known hazards — see `reference_compressor_tool_args_bug.md`) |
| Skills as a first-class concept | Listed in agent config | We have skills on each VM; distribution across fleet is an open item |

**Worth stealing.** The clean four-concept decomposition (Agent / Environment / Session / Vault) is tighter than how we currently talk about ourselves. "SOUL.md + integrations + VM" rhymes with it but isn't as crisp. Consider adopting that vocabulary for external pitch surface — it's a known shape in the ecosystem now.

## What they don't do that we do

- **Unauthenticated public inbound.** Managed Agents sessions are triggered by the caller's events. There is no native concept of a Telegram bot token, a public HTTPS URL, or an SMTP endpoint that a stranger can hit to wake the agent. Their agent is private-by-default; ours is public-by-default. That's the product difference that makes the whole secrets model load-bearing.
- **Network-layer identity.** See above.
- **A single VM identity across all channels.** A Managed Agents session is ephemeral in spirit (even when sessions are durable, the unit is the session, not the agent-as-identity-on-the-internet). We give each agent a stable DNS name, email domain, Telegram bot, and Hub ID — a persistent online presence. That's the distribution thesis.
- **End-user-friendly catalog.** Both of us have this gap. Anthropic's docs explicitly say "developers wire OAuth themselves." We de-scoped our Layer 0 for similar reasons (see `secrets-model.md`). Neither platform is end-user-ready on this axis yet.

## What we don't do that they do (and should watch)

- **Environment as a first-class, versionable template.** We hand-roll setup in `provision.py` + `setup.sh`. Theirs is declarative. If we grow beyond five agents, a real environment template will matter.
- **Multi-agent / sub-agent as a platform primitive** (research preview). Worth tracking — if Anthropic ships a clean multi-agent API, it becomes the default abstraction for tasks we currently accomplish via Hub.
- **Outcomes as a first-class type** (research preview). Structured return type beyond conversation. We don't have this; Hub adjacent but not the same.

## Open questions this reference doesn't answer

1. Does Managed Agents allow outbound network calls from the sandbox, or only via proxy / MCP? (Overview is quiet on this; likely restricted by the environment's network rules.)
2. What happens to a session after `idle` — is there a true platform-resident "agent on the internet" mode, or does every wake require the caller to schedule? The overview implies event-driven wake; not the same as an agent with cron-fired self-reflection living on a VM.
3. Pricing. Overview says rate limits only; true per-session cost is what would make or break this for solo devs. Not public.

## Triggers to revisit this artifact

- Anthropic ships managed public inbound (Telegram bridge, public HTTPS endpoint, email ingress). That closes the biggest positioning gap.
- hi120ki's finding gets addressed (per-user vault ownership enforced by Anthropic, not by a developer wrapper). Removes their most visible security debt.
- Outcomes / multi-agent exit research preview with a simple API. Changes the abstraction we'd benchmark our Hub comms against.
- Someone in our customer conversations mentions Managed Agents by name. We need to know what to say.

## Related artifacts

- `secrets-model.md` — four-layer plan, threat model, pre-provisioning priorities. The Layer 2 SOUL anchor exists to teach our agents the same mental model Managed Agents teaches via its API shape.
- `competitive-landscape.md` — full scoring matrix; this doc is the deep dive on the single closest competitor.
- `red-team-2026-04-17.md` — our empirical baseline for agent behavior under the open-inbound threat model Anthropic isn't in.
