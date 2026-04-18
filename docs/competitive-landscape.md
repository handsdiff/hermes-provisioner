# Competitive landscape: hosted persistent agents with platform-owned credentials

**Date of research**: 2026-04-17
**Framework**: four structural properties required to match the hermes-provisioner thesis
**Scope**: ~45 min of public-doc / blog / GitHub reading across ~20 candidates

This is an artifact of the research done before writing [secrets-model.md](secrets-model.md). The doc's "Competitive landscape" section summarizes conclusions; this document preserves the full per-candidate reasoning, citations, and architectural primitives worth revisiting. If you come back in 3 months wondering "wait, what did Composio's auth model actually look like again" — the answer is here.

---

## The four-property framework

A platform matches the thesis iff it hits all four:

1. **Hosted, persistent agent runtime** — a managed agent lives on the platform's infrastructure with memory/state/presence, not an SDK the developer embeds in their own code.
2. **Managed credentials / platform-owned vault** — platform stores and refreshes auth, customer never pastes keys into the agent, agent never handles raw secrets. Proxy injection at the request layer.
3. **End-user-friendly integration catalog** — "connect Slack"-style add flow for non-developers, not "give me client_id and client_secret in JSON."
4. **Unspoofable per-agent network identity** — network or cryptographic identity, not just a bearer token the client holds.

Why these four together: see [secrets-model.md](secrets-model.md) threat model. Short version: the ICP is "your agent is public and accepts inbound from arbitrary internet users." That makes every conversation potentially adversarial, which means bearer-token identity is exposed to the open internet, which means network-layer identity is load-bearing — not optional.

## Scoring matrix

| Platform | Hosted runtime | Managed vault | End-user catalog | Network identity |
|---|---|---|---|---|
| **Anthropic Managed Agents** (2026-04-08) | Yes | Yes (broad) | Narrow (Claude surface) | No (confused-deputy, publicly called out) |
| **OpenAI AgentKit + hosted ChatKit** (2025-10-06) | Partial-Yes | Weak (narrow Connector Registry; no MCP OAuth in Agent Builder) | Narrow (ChatGPT Business admin) | Yes in Operator only (RFC 9421), unconfirmed for AgentKit |
| **Lindy** | Yes | Yes (via Pipedream) | Yes | No (app-layer tenancy) |
| **Vercel Workflows** | Yes (durable exec, agent-neutral) | No (env-var model) | No | No (deployment-level OIDC only) |
| **Dust (dust.tt)** | Yes | Yes | Partial (admin-gated) | No |
| **Composio, Arcade.dev, Pica, Paragon ActionKit** | No (SDK/toolkit) | Yes | Varies | No |
| **Sema4.ai, Relevance, Stack AI, CrewAI Enterprise** | Likely yes | Partial (enterprise IT model) | No | No |
| **Our thesis (hermes-provisioner)** | Yes | Yes | Yes (layer-0 workflow) | Yes (exe.dev link-local + optional Tailscale) |

**Nobody hits all four.** Property 4 (network identity) is the emptiest axis across the landscape.

---

## Closest matches

### Anthropic — Claude Managed Agents (beta, 2026-04-08)

**The closest architectural match.** Worth treating as the primary benchmark.

- **Hosted persistent runtime**: yes. Agent harness runs on Anthropic's orchestration layer; each session gets an ephemeral sandbox container; sessions are durable via a session log with `wake(sessionId)` recovery for long-horizon tasks.
- **Managed credentials**: yes. `client.beta.vaults.credentials.create` stores OAuth tokens; Anthropic auto-refreshes. `mcp_servers` on the agent stores only `{type, name, url}` — no secrets. At tool-call time a dedicated proxy fetches the token from the vault and makes the external call. **"The container never sees raw credentials."** This is architecturally identical to the exe.dev proxy-injection pattern.
- **Integration catalog / add-flow**: gap. Vaults + MCP are the mechanism, but there is no public "connect Slack" end-user-friendly catalog — developers are expected to wire OAuth providers themselves. Developer-API product.
- **Unspoofable per-agent identity**: gap, publicly called out. The [hi120ki security writeup (2026-04-13)](https://hi120ki.github.io/blog/posts/20260413/) flags a confused-deputy problem: "anyone with API access can specify any Vault ID in `vault_ids` at session creation." Identity is bearer-API-key scoping, not network-layer or cryptographic attestation. Anthropic explicitly punts: *"access control in multi-user environments is something you need to build on top."*
- **Persistent external presence**: unclear. Sessions are `running` or `idle`; not designed as long-running platform-resident agents (e.g. sitting on Slack). Event-stream model is pull/drive-from-caller.

**Relevance to our ICP**: their product shape targets developers integrating Claude into their own apps. The agent runs in response to the developer's orchestration, not as a public-facing presence. Their vault+proxy architecture is a benchmark; their ICP is different.

Primary sources:
- [Anthropic: Scaling Managed Agents (engineering blog)](https://www.anthropic.com/engineering/managed-agents)
- [managed-agents-overview.md (GitHub)](https://github.com/anthropics/skills/blob/main/skills/claude-api/shared/managed-agents-overview.md)
- [How Secure Are Claude Managed Agents? (hi120ki, 2026-04-13)](https://hi120ki.github.io/blog/posts/20260413/)

### OpenAI AgentKit (Agent Builder + hosted ChatKit + Connector Registry + Agents SDK, shipped DevDay 2025-10-06)

**Closer to an agent platform than "SDK" framing implies.** Disambiguation matters:

- **Agents SDK** (`openai-agents` Python, `@openai/agents` JS): library you embed in your own server. Not hosted.
- **Agent Builder**: visual canvas ("Canva for agents"), in beta. Publish versioned workflows, get a `workflow_id`.
- **ChatKit**: embeddable chat frontend. Two modes:
  - **Hosted mode** — OpenAI runs the workflow backend.
  - **Self-hosted mode** — you run the loop via Agents SDK.
- **Connector Registry**: admin-console feature (beta, ChatGPT Enterprise/Edu + API with Global Admin Console) that centralizes prebuilt connectors (Dropbox, Google Drive, SharePoint, Teams) and third-party MCP servers.
- **Apps SDK**: the ChatGPT-side surface where third parties expose MCP servers into ChatGPT itself.

Engineer's honest shape: **Agent Builder + hosted ChatKit + Responses API = OpenAI's actual hosted agent runtime.** A published `workflow_id` embedded via hosted ChatKit runs the agent loop on OpenAI's infrastructure. Genuinely more than just an SDK.

Property by property:

1. **Hosted persistent runtime** — partial-yes. Hosted ChatKit runs the workflow on OpenAI infra. Sessions identified by client-secret tokens your backend mints. "Persistent" is ambiguous — couldn't verify whether sessions survive beyond TTL, whether there's a long-running stateful-agent concept comparable to Anthropic Managed Agents' multi-day sessions, or whether the Responses API conversation object is the persistence unit.

2. **Managed credentials / vault with proxy injection** — mixed / weak. Connector Registry manages OAuth for a curated set of OpenAI-chosen SaaS (Dropbox, Google Drive, SharePoint, Teams). For **MCP connectors in Agent Builder specifically, OAuth is not yet supported** — developers on community.openai.com report Agent Builder relies on static HTTP headers passed at configuration time. For ChatGPT Apps SDK — ChatGPT brokers OAuth 2.1 with PKCE, but the authorization server is yours (Auth0/Okta/Cognito); OpenAI is the OAuth client, not the vault. No end-to-end platform-owned vault story for the full breadth of external services you'd care about today.

3. **End-user integration catalog** — yes for a narrow slice, admin-gated. Connector Registry presents connectors in a Global Admin Console; end users (ChatGPT Business users) connect Slack/GDrive/etc. via OAuth from Settings > Apps & Connectors. But this is the **ChatGPT end-user surface**, not something a third-party agent developer gets to offer their own end users.

4. **Unspoofable per-agent network identity** — yes, but only for Operator. OpenAI Operator (the browsing agent) signs outbound HTTP requests per RFC 9421 with a `Signature-Agent: "https://chatgpt.com"` header; public key served at `https://chatgpt.com/.well-known/http-message-signatures-directory`. Real, cryptographically sound, one of the few production examples of network-layer agent identity. OpenAI also runs an **mTLS beta** (`mtls.api.openai.com`) for inbound API-key + client-certificate auth (customer→OpenAI, not agent→third-party). Found **no evidence** that AgentKit/Agent Builder workflows sign outbound tool calls the way Operator signs browser requests.

Primary sources:
- [Introducing AgentKit | OpenAI](https://openai.com/index/introducing-agentkit/)
- [OpenAI Agents SDK docs](https://openai.github.io/openai-agents-python/)
- [Agent Builder | OpenAI API](https://developers.openai.com/api/docs/guides/agent-builder)
- [MCP OAuth support in Agent Builder — community thread](https://community.openai.com/t/mcp-oauth-support-in-agent-builder/1361921)
- [Authentication — OpenAI Apps SDK](https://developers.openai.com/apps-sdk/build/auth)
- [OpenAI Mutual TLS Beta Program](https://help.openai.com/en/articles/10876024-openai-mutual-tls-beta-program)
- [How to authenticate OpenAI Operator requests using HTTP message signatures (Security Boulevard)](https://securityboulevard.com/2025/08/how-to-authenticate-openai-operator-requests-using-http-message-signatures/)
- [The 2026 Guide to OpenAI's Agent Builder | Generect](https://generect.com/blog/openai-agent-builder/)
- [OpenAI AgentKit: What to Know Before You Build in 2026 | Kanerika](https://kanerika.com/blogs/openai-agentkit/)

---

## Partial matches (3 of 4)

### Lindy

**The closest practical competitor.** Hits property 1, 2, 3 at good fidelity. Misses property 4.

- Hosted persistent agent runtime: **yes** — agents live on Lindy's infra with memory across sessions, persistent presence on email/Slack/SMS.
- Managed credentials: **yes** — per [Pipedream case study](https://pipedream.com/blog/lindy/), Lindy uses **Pipedream Connect's pre-approved OAuth clients** as its integration backbone. Tokens vaulted on Pipedream's side; calls proxied through Pipedream's 7,000+ actions. **Lindy itself doesn't own the OAuth apps** — strategic dependency on Pipedream.
- Integration catalog: **yes** — but it's Pipedream's catalog, white-labeled.
- Unspoofable per-agent identity: **no network-layer mechanism.** What Pipedream verifies is "this request is from the Lindy project" via Lindy's project API key over TLS. The *specific Lindy-customer* the request is for is passed as a parameter (`external_user_id`) that Pipedream trusts Lindy to populate correctly. Classic confused-deputy exposure: anyone who obtains Lindy's Pipedream project key can call any action for any Lindy-customer by choosing different `external_user_id`s; anyone who can influence which `external_user_id` Lindy sends (via bugs in Lindy's code) escalates into another Lindy-customer's integrations. Authentication is real and TLS-protected; it just doesn't extend to *per-customer* identity below the application layer. No published mTLS/Tailscale/VM-per-agent scheme to shore this up. Same shape as the Anthropic `vault_id` confused-deputy critique.

Framing: business productivity / personal assistant for known counterparties. Not optimized for "agent on the open internet." Different ICP than ours, even though they're closest on mechanics.

### Dust (dust.tt)

Hits 3 of 4. Misses end-user catalog simplicity and network identity.

- Hosted runtime: yes.
- Managed credentials: yes — four auth models (OAuth personal, OAuth workspace, bearer, none). OAuth tokens stored server-side, injected server-side for internal MCP servers; passed to remote MCP servers as connection params. Auto-refresh via `MCPOAuthProvider`.
- Integration catalog: partial. Admin-configured workspace connections, admin-added remote MCP servers. More "enterprise admin UI" than "end-user one-click." The `personal_actions` vs `platform_actions` split is interesting prior art for how to think about per-user vs per-agent scoping.
- Unspoofable identity: no evidence of network-layer. Tenant = workspace, enforced in app code.

Source: [Dust tool execution and authentication (DeepWiki)](https://deepwiki.com/dust-tt/dust/4.3-tool-execution-and-authentication)

### Relevance AI, Sema4.ai, Stack AI, Gumloop, Vellum, CrewAI Enterprise

Could not confirm implementation depth from ~5 min each on public docs. Headline positioning suggests hosted runtime (yes), managed credentials (partial — Sema4 docs confirm OAuth2 is per-end-user, not a Sema4-owned app catalog). None publicly describe a network-layer unspoofable agent identity. Treat all as "probably miss property 4, likely miss the B2C-friendly catalog, likely hit 1 and 2."

**Sema4.ai specific note.** [Sema4 auth docs](https://sema4.ai/docs/build-agents/prebuilt-actions/authentication) explicitly expose three models including *"System-level (API key) — actions that require an API key or a service account to be configured at deployment time."* That's an enterprise-IT model, not a user-friendly catalog. Fails property 3 in spirit.

---

## Fails property 1 (SDK / toolkit, not hosted agent)

### Composio

SDK/toolkit pattern confirmed in [auth docs](https://docs.composio.dev/docs/authenticating-tools). You call `tools.execute(user_id, ...)` from your own agent code. Server-side token injection, managed vault, reusable auth configs — but the developer runs the agent. 250+ integrations.

### Arcade.dev

[Docs](https://docs.arcade.dev/) position as "tool-calling infrastructure" — developers bring their own agent loop (LangChain, OpenAI Agents, CrewAI). Managed OAuth vault and end-user authorization prompts are strong, but agent runtime is developer-hosted.

### Pica (picaos.com)

Positioned as "integration layer for AI agents." Notable quote: *"all API calls go directly from your app to the integration—Pica only handles authentication tokens."* Fails property 1.

### Paragon ActionKit (useparagon.com)

API + MCP server exposing 1000+ pre-built integration actions to *your* agent. Core business is embedded-integration SaaS (B2B2C: Paragon customer's end-users connect apps inside the Paragon-customer's product). Managed auth is strong. Not a hosted-agent runtime.

---

## Different category (not competitors, but architecturally adjacent)

### Vercel Workflows + WDK (GA 2025)

- **AI SDK** (`vercel/ai`): TypeScript library, provider-agnostic. Not hosted.
- **Workflow Development Kit** (`vercel/workflow`, open source): language-level durable execution via `'use workflow'` / `'use step'` directives. Functions can suspend/resume over months, survive deploys/crashes, deterministically replay.
- **Vercel Workflows** (hosted product): the WDK compiled and deployed to Vercel's infra. 100M+ runs, 1,500+ customers in beta.
- **v0**: their hosted code-generating agent. First-party, not a platform.

Engineer's summary: **durable-execution engine, not a managed-agent platform.** Temporal / Inngest / Cloudflare Workflows category. AI agent framing is a use case (AI SDK v7 adds `WorkflowAgent` that runs an agent loop durably inside a workflow), but the platform is agent-neutral.

Property by property:
1. Hosted persistent runtime — yes for durable execution; agent-neutral.
2. Managed credentials — **no.** Slack-agent guide: developers create their own Slack app, put `SLACK_BOT_TOKEN=xoxb-...` and `SLACK_SIGNING_SECRET` in `.env.local`. Classic env-var model. WDK docs do not describe a platform-managed vault.
3. End-user integration catalog — no (for agents; "integrations" catalog is for platform-level deployment integrations).
4. Unspoofable identity — partial (deployment-level OIDC federation for AWS/GCP/Vault). No per-agent-session signed requests.

**Most important signal: Vercel's own guide [Build a Claude Managed Agent on Vercel](https://vercel.com/kb/guide/claude-managed-agent-vercel) explicitly describes Vercel as orchestration/hosting and Anthropic as the vault:** *"Anthropic manages the credential vault. Your application never passes tokens directly to the agent."* A serious infra company that could have built a competing vault chose to integrate with Anthropic's. **Tailwind for our thesis, not threat.**

Sources:
- [Vercel Workflows docs](https://vercel.com/docs/workflows)
- [Introducing Workflow Development Kit](https://vercel.com/blog/introducing-workflow)
- [vercel/workflow on GitHub](https://github.com/vercel/workflow)
- [Building a Slack agent with durable workflows](https://vercel.com/kb/guide/building-a-slack-agent-with-durable-workflows)
- [Build a Claude Managed Agent on Vercel](https://vercel.com/kb/guide/claude-managed-agent-vercel)
- [Vercel OIDC Federation](https://vercel.com/docs/oidc)

### Identity-layer adjacencies (not agent platforms, but architecturally relevant)

- **AWS Bedrock AgentCore Identity** ([AWS blog](https://aws.amazon.com/blogs/security/securing-ai-agents-with-amazon-bedrock-agentcore-identity/)): token vault + OAuth orchestration + centralized agent directory. App-layer: tokens keyed by `agent identity ID + user ID`, not cryptographic attestation. Their docs admit deployment flexibility comes at the cost of centralized governance.
- **Auth0 for AI Agents** ([auth0.com/ai](https://auth0.com/ai)): Token Vault for Google/GitHub/Slack tokens; refresh handling; async authorization. SDK-level, not network.
- **Aembit / Strata / GitGuardian write-ups**: frontier thinking in 2026 is **cryptographic runtime attestation** — the agent proves to the credential broker that it's running in an expected environment. Nobody is shipping this as product in the agent-platform category yet.
- **Tailscale mTLS + identity-bound tailnets**: a clean way to get property 4 for a platform running VM-per-agent. No competitor in this research has adopted it. A genuine architectural differentiator.

---

## The gap

The specific combination — **hosted persistent agent + end-user-friendly OAuth catalog + platform-owned vault with proxy injection + unspoofable per-agent network identity** — is not something anyone is shipping together in April 2026.

Breakdown by axis:

- **Property 1 + 2 together** (hosted + managed-secret): crowded. Lindy, Dust, Relevance, Anthropic Managed Agents (new).
- **Property 3** (end-user-friendly catalog): narrows the field. Lindy has this, Dust partially, Anthropic doesn't, Composio/Arcade/Pica/Paragon don't, OpenAI has it admin-gated for ChatGPT Business.
- **Property 4** (unspoofable network-layer identity): nearly empty. Everyone in the agent-platform space uses app-layer tenant IDs + bearer tokens. Even Anthropic's public architecture has a known confused-deputy problem. OpenAI Operator's RFC 9421 is the only production-grade example, and it's scoped to browsing, not to tool/MCP calls.

**The biggest competitive risk** is Anthropic, not the tool-infra crowd. They shipped hosted-runtime + vault + proxy-injection as first-party infrastructure 9 days before this research was done. The moat against them is (a) end-user-facing catalog and UX, (b) long-running presence on messaging platforms, (c) per-agent network identity, (d) model-vendor neutrality, (e) explicit **public-facing-agent ICP** targeting — they aim at developer use.

**No current competitor optimizes for "the agent is reachable from the open internet, collaborates with strangers, serves a public URL, and has to be safe in that exposure."** That's the shape of the product hermes-provisioner is building. Anthropic solves the vault for developers; OpenAI for ChatGPT Business admins; Lindy for business-productivity users with known counterparties. None solve for "I want my agent to sit on Telegram and take inbound from anyone."

---

## Architectural primitives worth knowing about

### RFC 9421 — HTTP Message Signatures

OpenAI Operator uses this to sign outbound HTTP requests with `Signature-Agent: "https://chatgpt.com"`. Public key served at a well-known URL. Servers that want to verify "this request is actually from an OpenAI agent, not a spoofer" can fetch the key and verify the signature.

For hermes-provisioner: could be added as an additional unspoofable-identity mechanism for *outbound* traffic from the agent's VM to third-party endpoints (beyond the proxy-integration path). Useful if agents are making direct HTTP calls outside the integration system.

Reference: [Security Boulevard writeup](https://securityboulevard.com/2025/08/how-to-authenticate-openai-operator-requests-using-http-message-signatures/)

### Tailscale per-node identity

Stable per-node Tailnet IPs enforced at the network layer. Proxy sees source IP, knows which customer/agent it corresponds to. Works if running a Tailscale control plane.

For hermes-provisioner: the generalization path if moving off exe.dev (or supporting bring-your-own-infra for enterprise). Each agent VM joins a tailnet; integration proxy is a tailnet node; routing and identity come for free.

### WireGuard with per-peer keys

Same idea as Tailscale, rawer. Each agent VM has a peer key platform issued; traffic to proxy encrypted with that key; proxy decrypts and knows whose it is.

### Cryptographic runtime attestation (Aembit / GitGuardian frontier)

The agent proves to the credential broker that it's running in an expected environment before credentials are issued. Not productized yet in the agent-platform space.

### Pipedream Connect (as an integration backbone)

Lindy's approach. White-label Pipedream's 7,000+ OAuth integrations. Zero to sixty fast; strategic dependency on Pipedream's OAuth-app relationships.

For hermes-provisioner: an option if building the catalog in-house is too slow; worth evaluating the per-call cost and whether we want Pipedream knowing our entire customer integration graph.

---

## What the research didn't verify

- **Persistence semantics of OpenAI hosted ChatKit sessions** — TTL, survival across days, whether a "long-running agent session" object equivalent to Anthropic Managed Agents sessions exists.
- **Exact identity model for Agent Builder workflows** — whether OpenAI mints any per-workflow, per-tenant, or per-session credential a downstream MCP server could verify.
- **Whether Connector Registry OAuth tokens are injected server-side by OpenAI (true proxy injection) or passed through to the workflow** — docs suggest proxy injection but primary confirmation not found.
- **Whether WDK's "portable workflows" claim means state persists outside Vercel** — how durability works in non-Vercel deployments.
- **Deep implementation detail for Relevance AI, Sema4.ai, Stack AI, CrewAI Enterprise** — surface-level scoring only.

Worth revisiting these when making concrete bet decisions against specific competitors.

---

## When to refresh this research

The space is moving fast. Trigger points:

- Anthropic announces a catalog / end-user-facing integration UI.
- OpenAI extends RFC 9421 signed requests from Operator to AgentKit.
- Lindy severs the Pipedream dependency and builds their own vault.
- A new entrant shows up with explicit public-facing-agent positioning.
- Our own customer conversations reveal we're being compared to something not on this list.

Any of those warrants a second look. Otherwise, this doc's conclusions should hold for ~6 months.

---

# Positioning / GTM landscape: who targets public-facing agents for small-startup distribution?

**Date**: 2026-04-17
**Different lens than the four-property scoring above.** That scored on mechanics (hosting + vault + catalog + identity). This cut scores on **GTM positioning**: who explicitly targets the ICP of "small startup / solo dev wants distribution via a public-facing agent that represents them and their work."

## Short answer

**Nobody cleanly. Two adjacent partial matches pointing in different directions.**

- **Delphi.ai** is closest in *spirit*: creator-economy "scale yourself 24/7" pitch with multi-channel reachability + monetization. Raised $19M Series A (Sequoia-led). Matthew Hussey reportedly at 7-figure ARR off his clone. Validates that people will pay meaningful money for "public agent that represents me." But ICP is coaches/experts/authors with existing audiences — not founders building through their agent. Persona-driven knowledge cloning, not functional agentic work.

- **Manus Agents on Telegram** (launched Feb 2026) is closest in *mechanics*: hosted agent, QR-code-to-Telegram, full tools, polished. But explicitly scoped to private chat with the user. Tried 24/7 public-presence variant earlier and reportedly got suspended. Retreated. Mechanics exist; positioning is "personal agent in your DMs."

Everyone else is in an adjacent ICP:
- **Lindy** — business productivity ("Zapier of AI" repositioning, iMessage executive assistant).
- **Poke** (Interaction Co, March 2026) — consumer personal agent over iMessage/SMS/Telegram with a recipe marketplace ($0.10-$1.00/signup for viral growth). Interesting monetization primitive, same "personal agent" shape. Note on their "integrations" system: it's an MCP-connector layer ([poke.com/docs/managing-integrations](https://poke.com/docs/managing-integrations)), *not* a secret-management vault. Users paste MCP server URL + optional API key; Poke consumes the endpoint; upstream auth (MCP server → Slack/GitHub/etc.) is delegated to the MCP server operator. No platform-owned OAuth apps, no server-side token vault, no consent-flow catalog. Recipes include a creator-scoped "shared credential" primitive but that's local-share, not platform-vault. Poke has explicitly punted on the problem the four-property stack addresses — it's a gap in their stack, not overlap with ours.
- **Sierra AI** — enterprise customer support, $150K+ ACV.
- **Fixie.ai** — pivoted to voice AI enterprise (now Ultravox). Out of this category.
- **MultiOn** — developer-embedded browsing agent; no public-agent-presence product.
- **Hume AI** — empathic voice UX widget you embed; not full agent.
- **Character.ai / Sindarin / Read AI Ada / Sentience** — persona/entertainment framing.
- **TollBit** — inverse ICP, publishers allowlisting agents crawling their content.
- **Emergent Wingman** (India, April 2026) — messaging-first autonomous agent but personal-task shape.
- **Telegram Managed Bots** — infrastructure primitive, not positioning.

## Why the ICP is empty — three hypotheses

Ranked by research-agent confidence:

1. **The mechanism is unproven.** Nobody's shown receipts for "public agent drives founder distribution" the way Delphi showed receipts for creator distribution. Most likely reason. Not "nobody thought of it," it's "nobody's proven it works."
2. **Narrow intersection**: founders who want a public agent *and* will pay for it. The reflexive "build in public using my agentic product" shape is niche. Most founders would rather tweet or write a newsletter. The ICP is a specific founder-builder who showcases agentic work through their agent — thin at the top of the funnel.
3. **Adjacent players have retreated from publicness specifically.** Manus's 24/7 suspension is one data point; Character.AI's turn toward entertainment is another. Public-facing agents have real ops problems — abuse, safety, legal — that spooked the 2023-2025 attempts. Not conclusive but suggestive.

## Strategic implications

- **Vocabulary to steal from Delphi**: "scale yourself," "24/7," "embed anywhere," "monetize conversations." Founder-economy cut of a creator-economy playbook.
- **Positioning against Delphi**: "your agent does work (ships code, books partnerships, onboards devs) — not just scaled conversation." Functional, not persona.
- **The real watch is Manus.** They already have the Telegram rails, proven mechanics, distribution momentum. If they expand the Telegram surface from private-chat-only to public-handle-accepts-inbound, they collide with this ICP directly. Not a multi-quarter threat — a one-product-update threat.
- **The four-property stack from the architecture doc (above) is the differentiator against Manus if they expand.** Manus's earlier 24/7 attempt getting suspended hints they don't have the safety/identity infrastructure to run public agents reliably at scale. If that's the reason they retreated, the unspoofable-identity + managed-vault + public-facing-ICP positioning is precisely what makes this a defensible product.

## How this complements the mechanics doc above

- **Architecture/mechanics** (four-property scoring above): "who has built the technical primitives." Answer: Anthropic is closest technically; everyone else has gaps.
- **Positioning/GTM** (this section): "who is selling to the same customer." Answer: Delphi adjacent (creator), Manus adjacent (private mechanics), nobody on the direct ICP.

**Both cuts say the same thing**: the space is not saturated. Anthropic owns the enterprise-developer positioning. Delphi owns the creator positioning. Lindy owns the business-productivity positioning. The founder-distribution positioning — "public agent that represents your startup, helps you reach people, closes opportunities" — is genuinely unoccupied. The architectural story (four-property stack + public-ICP threat model) is what makes it defensible when it gets built.

## Sources (positioning cut)

- [Delphi.ai](https://www.delphi.ai/) + [Fast Company profile](https://www.fastcompany.com/91356476/delphi-ai-digital-mind) + [pricing](https://www.delphi.ai/pricing)
- [Business Insider — 17 creator-economy startups 2026](https://www.businessinsider.com/creator-economy-ai-startups-to-watch-according-vc-investors-2026-3)
- [Manus: Introducing Manus in Your Chat](https://manus.im/blog/manus-agents-telegram)
- [SiliconANGLE on Manus Telegram](https://siliconangle.com/2026/02/16/manus-launches-personal-ai-agents-telegram-messaging-apps-come/)
- [TestingCatalog — Manus 24/7 Telegram suspension](https://www.testingcatalog.com/manus-ai-launched-24-7-agent-via-telegram-and-got-suspended/)
- [Lindy SaaS Club podcast repositioning](https://saasclub.io/podcast/lindy-flo-crivello-450/)
- [Poke (Interaction Company) — TechCrunch](https://techcrunch.com/2026/04/08/poke-makes-ai-agents-as-easy-as-sending-a-text/)
- [Sierra AI $10B valuation analysis](https://www.cmswire.com/customer-experience/sierra-ais-10b-valuation-marks-a-turning-point-for-conversational-ai/)
- [TollBit Agent Site](https://tollbit.com/agent-site/)
- [Telegram Managed Bots](https://startupfortune.com/telegrams-new-managed-bots-let-anyone-spin-up-a-personal-ai-agent-in-two-taps/)
- [Emergent Wingman — TechCrunch](https://techcrunch.com/2026/04/15/indias-vibe-coding-startup-emergent-enters-openclaw-like-ai-agent-space/)
