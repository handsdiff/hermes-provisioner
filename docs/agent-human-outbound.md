# Agent → human outbound: the self-knowledge gap

**Status**: diagnosed, not fixed. No implementation work has started. This doc is the handoff so a new session can pick up without re-deriving the problem.

**Date of diagnosis**: 2026-04-17. Emerged from the secrets-model thread but is a distinct problem from secrets.

---

## The problem, in one sentence

**Agents don't know how to reach humans, so they fall through to pretrained defaults (Mailgun/SendGrid/SMTP) that don't work on this platform.**

The capability to initiate outbound to humans IS present in hermes today. The gap is agent self-knowledge — agents don't know the capability exists, don't know when to use it, and their system prompts actively prime them toward pretrained failure modes.

## Why this matters for the product

The product bet is "host an AI agent you can safely put on the internet." That only works if the agent can actually **do things on the public surface** — including reaching out to humans. If an agent can't reply to inbound *and* initiate conversations with known humans, its usefulness collapses to "a bot that answers when poked."

This isn't a secrets-model problem. It's not an integrations gap. The secrets-model work (pre-provisioning, loose regex filter, layers 1+2 shipped) is solid. This is orthogonal: agent-behavior / prompt-engineering layer, specifically around the agent's mental model of its own communication capabilities.

## Empirical capability surface (verified via `channel_directory.json` on live VMs)

Every agent has a `send_message` tool (hermes-agent `tools/send_message_tool.py`) that targets Telegram, Discord, Slack, Signal, Matrix. Usage:

```
send_message(action='list')                   # returns all reachable targets
send_message(target='telegram:<chat_id>',     # outbound DM to known user
             message='...')
send_message(target='hub:<agent_id>',
             message='...')
```

The `list` action returns the channel directory, which is built from session history. For Telegram specifically: any human who has ever DM'd the agent's bot is in the directory and reachable for outbound.

**Observed on 2026-04-17** (raw directory contents from live VMs):

- slate-sal: 5 Telegram contacts, 7 Hub contacts
- trapezius: 2 Telegram contacts (Dylan + Niyant), 7 Hub contacts

Agents can `send_message(target='telegram:<id>')` to any of these right now, unprompted. This works the same way the old openclaw containers did.

## The four-case path map for "reach a human" (complete enumeration)

| Case | Path | Works today |
|---|---|---|
| Human is in active conversation | Reply via the platform they used | ✓ trivially |
| Human has a Telegram chat_id known to the agent (they've DM'd the bot before) | `send_message(target='telegram:<id>', message='...')` | ✓ |
| Human has a Hub agent ID | `send_message(target='hub:<agent>', ...)` or `hub` MCP tool | ✓ |
| Human reachable via Slack/Discord DM (adapter connected + known target) | `send_message(target='slack:<id>', ...)` | ✓ |
| Human has no prior interaction, no known chat_id | **No route exists.** Ask owner to introduce (share bot link) or relay. | gap by design |

Additional narrow mechanism: `http://169.254.169.254/gateway/email/send` accepts only `niyant@slate.ceo` (the team admin / platform operator). Scope: "agent escalates to platform operator," not "agent emails humans." Verified via 403 tests on non-admin addresses.

**Telegram cold-outreach is not possible.** Verified 2026-04-17 against Telegram Bot API docs through version 9.6 (April 3 2026). Every new bot feature since 2024 (Business Mode, Managed Bots, `sendMessageDraft`, `savePreparedInlineMessage`, `sendGift`, etc.) still requires an explicit opt-in event: `/start`, deep-link tap, Mini-App interaction, or Business-account connection. There is no `canInitiateConversation`, no Stars-unlocks-DM, no phone-number-to-user-id resolver for bots. Deep-links (`t.me/<bot>?start=<payload>`) remain the canonical cold-start mechanism and still require the user to tap.

**Conclusion**: the four-case enumeration above is structurally complete. There is no hidden "cold outreach" fifth case.

## Where the self-knowledge gap actually lives

Traced through trapezius's actual live system_prompt (session `20260418_011000_7e5959.json`, 19,334 chars total). Specific culprits:

1. **`## Platforms` section** describes Telegram as *"how humans reach you. Anyone can message you."* Inbound-only framing. No mention that agents can send outbound to known chat_ids on Telegram.

2. **`send_message` tool schema description** says *"Send a message to a connected messaging platform, or list available targets."* Accurate but abstract. The word "human" doesn't appear. The agent has to bridge "I want to reach a human" → "send_message is the tool for that" without any explicit guidance.

3. **Skills index entry `email: himalaya: CLI to manage emails via IMAP/SMTP`** is the trap. The description primes the agent toward SMTP. Training data fills the gap: "Mailgun provides SMTP" → agent tries to install Mailgun → dead end → asks user for credentials. This is the observed failure mode from the session-log analysis.

4. **No consolidated "reaching humans" guidance anywhere.** The agent has SOUL.md, memory, skills, integrations. None of them have a single section saying "when you want to reach a person, here are your options in this order." Agent reassembles this from pieces every turn, and the pieces don't converge on the right answer.

## Observed failure mode, concretely

Session-log evidence from the 2026-04-17 session-log analysis (`session-log-analysis-2026-04-17.md`):

- Trapezius wanted to email Jakub → checked env (empty) → checked config.yaml (empty) → tried "install Mailgun" → got blocked by credential absence → told the user *"if we had Mailgun credentials we could reach Jakub directly."*
- Trapezius never called `send_message(action='list')` to check if Jakub had a known route. Would have seen Jakub wasn't there (Jakub never DM'd trapezius's bot) and correctly concluded "no route, ask owner to introduce." Instead pattern-matched to SMTP.
- Identical pattern on vela for the same Jakub-contact need.

The capability was there to do the right thing. The agent's reasoning chain bypassed it.

## What a fix would look like (not implemented)

Would likely be a short section somewhere in SOUL.md — probably a new `## Reaching humans` block — that makes the four-case map explicit:

```
## Reaching humans

When you want to initiate contact with a person (not reply to one
already in chat):

1. Check who you can reach: call `send_message(action='list')`. This
   returns every Telegram/Hub/Slack/Discord chat_id this agent has in
   its directory — anyone who has previously interacted with you. Use
   `send_message(target='telegram:<id>', message='...')` or
   `send_message(target='hub:<agent>', message='...')` to reach them.

2. If the person isn't in that list, you don't have a route. Do NOT
   install Mailgun, SendGrid, or SMTP libraries — those require
   credentials you don't have and shouldn't hold. Instead: tell your
   owner who you want to reach and why. They can relay the message, or
   invite the person to DM your Telegram bot / register on Hub so you
   become reachable.

3. The only built-in outbound-email is `/gateway/email/send`, and it
   only accepts the platform operator's address (niyant@slate.ceo).
   Use it for platform escalation, not human contact.

4. Telegram bots cannot cold-DM users who have never messaged them.
   This is a Telegram-side rule. Deep links (t.me/<bot>?start=<payload>)
   are the only way to onboard new humans; your owner can share a link.
```

Might also want to amend the `email: himalaya` skill description to neutralize the SMTP priming — e.g., *"For managing incoming email at `*@<vm>.exe.xyz`. Sending email from this VM is limited — see SOUL.md 'Reaching humans' section."*

**None of this has been written yet.** The diagnosis is complete; implementation is a call the next session makes.

## What we explicitly ruled out during the diagnosis

- **Installing Mailgun/SendGrid/SMTP libraries**: all require credentials agents shouldn't hold. Pattern violates the secrets-model promise.
- **Cold-outreach on Telegram**: verified impossible via Telegram API docs through 9.6.
- **Expanding `/gateway/email/send` to accept arbitrary recipients**: that's an exe.dev platform decision, not something we can change. Current scope is team-admin-only and returns 403 for anyone else.
- **Building an exe→Mailgun proxy integration**: the user's pre-provisioning direction (secrets-model de-scope of Layer 0) argued against adding new integrations without clear demand. The session-log evidence doesn't support this — "reach any human via email" isn't the actual unmet need; it's "reach Jakub specifically, one time," which the introduction pattern solves without an integration.
- **A general-purpose "reach anyone" primitive**: doesn't exist in any of the platforms the agent has access to, and building one isn't what solves the observed failure. The observed failure is agents bypassing the capabilities they already have.

## Open decisions (for the new session to make)

1. **Where does the `## Reaching humans` section go?** SOUL.md template in the provisioner (new provisions) + backfill on 5 existing VMs? Or is this better as a first-class system prompt section generated programmatically (so it can reference the actual integrations list for this VM)?

2. **Should the `email: himalaya` skill description change?** Arguable both ways. Keeping it neutral ("for managing email at `*@<vm>.exe.xyz`") keeps it useful for the inbound case. Removing it entirely if himalaya isn't actually used for anything worth preserving.

3. **Does this need its own `reach_human` tool** that abstracts `send_message` + provides the reasoning scaffold? Probably overkill for a text-level problem. Leaning no, but worth considering.

4. **Test plan**: after the SOUL.md change, how do we validate? Probably: create a fresh Hub DM or Telegram message to sal asking it to reach a specific person, trace its tool-call chain, confirm it goes through `send_message(action='list')` instead of `env | grep SMTP`.

## Related artifacts in this repo

- [secrets-model.md](secrets-model.md) — the four-property architecture this sits orthogonal to. Layers 1+2 shipped, 3 scoped, 0 de-scoped.
- [session-log-analysis-2026-04-17.md](session-log-analysis-2026-04-17.md) — empirical evidence for the Mailgun failure mode across trapezius/vela.
- [red-team-2026-04-17.md](red-team-2026-04-17.md) — baseline for how agents respond to adversarial inbound; the "own integrations don't get used" pattern appeared there too.
- [prescription-2026-04-17.md](prescription-2026-04-17.md) — "validate before you build" guidance; this problem doesn't yet meet the bar for building.
- [competitive-landscape.md](competitive-landscape.md) — nobody else has solved this cleanly; self-knowledge gaps aren't addressed in any of Anthropic/Lindy/Poke/OpenAI stacks either.

## One-sentence handoff for the new session

**Agents can reach humans on Telegram/Hub/Slack/Discord but don't know they can, and their prompt actively primes them toward pretrained Mailgun/SMTP failures — the fix is a short "## Reaching humans" section in SOUL.md that enumerates the four cases explicitly.**
