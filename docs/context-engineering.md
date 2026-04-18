# Context engineering — our read, applied to provisioned agents

**Source.** Anthropic Applied AI, *Effective Context Engineering for AI Agents* (2026). Captured here as our working interpretation, not a verbatim summary. Point of this doc: translate the framing into specific decisions for hermes-provisioner.

## The framing we're adopting

Prompt engineering = writing one set of instructions. Context engineering = curating what fills the model's context *on every turn* of a long-running agent. For a provisioned Hermes agent — which runs for days, accumulates session history, Hub DMs, Telegram messages, cron-fired self-reflection, skills, and durable memory — every turn is a fresh context-assembly problem. Prompt engineering is a special case; context engineering is the actual job.

**The constraint that makes this engineering and not prose.** LLMs have an attention budget, not just a window. Transformer attention is quadratic; longer sequences strain it; training distributions are shorter than the advertised max. Long contexts don't just cost more tokens — they cost *recall quality*. "Context rot" is real and measurable.

**The operative principle.** Find the smallest set of high-signal tokens that make the desired behavior likely. Every byte in the context must be earning its place.

## The six techniques, as they apply here

### 1. System prompt altitude

The prompt has to be specific enough to guide behavior, general enough not to hardcode brittle conditionals. Too vague = model falls through to pretrained defaults. Too specific = every new case needs a new branch.

**Our case.** SOUL.md is our altitude knob. Today it sets identity and values; it mostly does not tell the agent *what its tools are for*. This is the gap `agent-human-outbound.md` diagnosed — the prompt is correctly abstract about identity but silent on the concrete capability map, so the agent re-derives "how do I reach a human" from training data and lands on Mailgun. **The fix isn't more prompt — it's a targeted altitude shift on the one axis where training-data defaults are wrong.**

### 2. Tool curation

Bloated toolsets create ambiguous decision points. If a human couldn't pick the right tool from the description, the agent can't either.

**Our case.** The integration set per agent is already tight (hub, tg, db, x, slack, coda, litellm, honcho-retired, langfuse). The failure surface isn't "too many tools," it's that the `send_message` tool's description is too abstract for the agent to bridge "I want to reach a person" → "this is the tool." Tool curation here means **rewriting descriptions so the intent-to-tool mapping is obvious**, not pruning further.

### 3. Few-shot canonicals over edge-case enumeration

Examples do work; stuffing edge cases into prose doesn't. Pick canonical examples that show the shape of correct behavior and let the model generalize.

**Our case.** We don't currently ship canonical examples into agent context. The `## Reaching humans` block being designed in the outbound doc is effectively a four-row canonical table — that's the right shape. Apply the same pattern anywhere else the agent re-invents wrong (secrets handling, Hub discovery etiquette, responding to cold inbound).

### 4. Just-in-time retrieval over pre-loading

Move from "stuff everything into context" to "carry lightweight handles (paths, IDs, queries), fetch at runtime." Works better in practice, scales to arbitrary corpus sizes, and the metadata of the handles (filenames, folder structure, timestamps) is itself signal.

**Our case.** Hermes already does this on disk — skills index + file reads, grep, glob. The area we *under-use* it is memory: the Honcho purge pushed us toward file-based pull memory, which is exactly the JIT shape. The near-term win is making sure the file layout has strong metadata (good names, obvious folders) so an agent navigating its own memory can find things without reading everything. The agent-cards / specialization thread is the longer version of this.

### 5. Long-horizon survival — compaction vs. notes vs. sub-agents

Three distinct tools for long-running contexts, with different trade-offs:

- **Compaction.** Summarize history, restart. Risk: over-aggressive loss of subtle context. The 2026-04-17 `tool_args` corruption incident is a direct instance of this — our compressor dropped structure that turned out to matter. The lesson from the article we'd already learned the hard way: *tune for recall first, then precision.*
- **Structured notes.** Agent writes its own memory externally; pulls back as needed. This is our current direction post-Honcho.
- **Sub-agents.** Spawn focused sub-agents with clean contexts; parent synthesizes distilled returns. We don't do this yet. Candidates where it would pay: Hub discovery sweeps, multi-site red-team runs, batch email triage.

**Which fits when:** conversational back-and-forth → compaction. Iterative development with milestones → notes. Parallel exploration where results can be compressed → sub-agents.

### 6. Treat context as a finite budget even when the window is huge

Large context windows do not dissolve the problem; they just raise the threshold at which it bites. "Context pollution" applies at every window size. Discipline is the only durable answer.

## What this changes for us, concretely

- **SOUL.md becomes a context-engineering surface, not just a values statement.** The "## Reaching humans" block is the first canonical section; expect more (stranger-inbound behavior, secrets handling, Hub etiquette).
- **Tool descriptions are part of the prompt.** `send_message`, `integrations`, `hub` MCP tools need descriptions that bridge intent → tool without the model having to reason around pretrained defaults.
- **Memory file naming and layout are load-bearing.** JIT retrieval only works when the handles are informative. Skills and memory directory naming should be audited as we touch them.
- **Compaction is a known-hazardous operation.** We've already been burned once. When we write to the session store, assume future compaction may pass over it; structure content so the compressor can't silently mangle it (no JSON-in-prose, keep tool_call records opaque).
- **Sub-agent pattern is an available tool we haven't used.** Worth reaching for when a single agent's context would otherwise blow up on a task that naturally parallelizes.

## The explicit anti-patterns to watch for

1. Prompt growth as the default response to any new failure mode. Every addition taxes every future turn.
2. Adding tools to cover cases rather than making existing tools obvious.
3. Loading everything up-front "for safety." Up-front loading has a cost; JIT has a cost; pick per use case, don't default to pre-load.
4. Aggressive compaction without validation. Dropping tool results too early, over-summarizing, discarding what seemed redundant. Keep a rollback path.
5. Treating "the model is smart enough to figure it out" as an excuse for under-specified context. It sometimes is, and when it isn't, the failure is quiet and pattern-matched to training defaults.

## Related artifacts in this repo

- `agent-human-outbound.md` — the cleanest current instance of a context-engineering failure (agent bypassing a capability it has, because the prompt doesn't point at it).
- `reference_compressor_tool_args_bug.md` (memory) — the compaction-precision hazard, already encountered.
- `project_agent_specialization.md` (memory) — the longer thread on relational memory / agent cards; JIT retrieval is the mechanism.

## The one-line carry

**Context is a finite budget; curate it turn-by-turn with the smallest set of high-signal tokens that makes the right behavior likely. For us, that means SOUL.md does intent-to-tool mapping, tool descriptions earn their keep, memory layout is navigable, and compaction is treated as dangerous.**
