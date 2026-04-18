# Prescription: validate before you build (2026-04-17)

**Context.** After a day of competitive research + red-team + designing the secrets model, the strategic question surfaced: given the research + the README's discovery-first philosophy, what's the right next move? This doc captures the prescription that came out of that conversation. Intentionally blunt. Keep as a reference anchor when the temptation returns to ship more infrastructure without new customer signal.

## Stop building. Start validating.

The research changed the calculus in three concrete ways:

1. **The space we thought we were entering is genuinely unoccupied** for the ICP — "safe publicness for founder distribution." Good news, and no competitor needs to be out-shipped in a panic.
2. **The core architectural wins are already shipped or demonstrably working.** Layer 1+2 deployed, red team didn't surface catastrophic leaks, the exe.dev integration model does what we want it to. The *promise* of the platform is real today.
3. **The piece we don't have evidence for is the piece the research can't answer**: does the ICP actually exist at scale, and do they actually want this? Delphi proved creator-economy willingness-to-pay. Nobody has proved founder-economy willingness-to-pay. Manus retreated from public-agent mode for reasons nobody has publicly explained.

In that context, the next two pieces of engineering work we talked about — Layer 3 (write-filter) and Layer 0 (add-integration workflow) — are **optimizing the promise**, not **validating the demand**. They're defensible moves *if* we have signal. Today we don't.

## What to actually do

**In priority order, stop at the top of the list that produces enough signal.**

1. **Ship the X article on public agents** (already on the roadmap). Not as marketing — as a **signal-gathering instrument**. Frame explicitly: "your agent, on the open internet, representing your work." See who responds. Founder DMs are the validation event.

2. **Put sal (or a platform-rep agent) on a public Telegram handle** and post it with a specific hook. Not "try my agent" — something with a stake: *"this agent knows about $X, ask it."* Measure inbound quality and volume over 2 weeks. Cheapest single answer to "does anyone want this?"

3. **Ask the five existing agent owners** (Dylan/Jakub/Adam/Darryn) for raw receipts. Concrete: has your agent produced any public-facing value you wouldn't have had otherwise? If the answer is no from everyone who already has one, the ICP isn't the problem — the product's value doesn't show up yet.

4. **Define "enough signal" upfront so you don't rationalize later.** Something like: *"if ≥3 founders outside the current owner set message unprompted about their own work in 2 weeks → thesis validated, ship Layer 0 seriously. If not → reassess what's actually failing."*

## What to defer

- **Layer 3 (write-side filter).** Justified by one data point (vela's Solana privkey). Zero observed exploitation. Design is captured in `secrets-model.md`. Ship when either (a) a real at-rest leak incident happens, or (b) you're doing a security-positioning marketing push and want the "airtight" claim. Don't ship on speculation.
- **Layer 0 full catalog + OAuth flows.** Real work. Justified by hypothetical customer demand we haven't measured. Ship when 2-3 customers ask for the same integration that isn't provisioned, or when market signal validates the ICP enough to invest.
- **Network-identity hardening (Tailscale/mTLS).** Major infra lift. Justified by a threat model no customer has asked about yet. Ship when a specific customer objection or compliance requirement arrives. Not before.

## What the last few hours actually produced

Not wasted — the research + docs unlocked three things that matter more than the code we might have written:

- **A defensible positioning** — *"host an agent you can safely put on the internet."* Concrete, differentiated from every competitor found in research, grounded in architecture already in place.
- **A gap map** — we now know exactly where Anthropic, Lindy, Delphi, OpenAI, Manus, Poke sit. That determines how to pitch against them when customer conversations happen.
- **Permission to not ship** — the secrets-model doc's validation plan + competitive framing both support *"ship when signal appears."* Before today, the instinct was to keep building. Now there's a written artifact that says stopping is the right move.

## The explicit trade

Trading a week of Layer 3 / Layer 0 implementation work for two weeks of public-market signal gathering. If the signal is good, ship the deferred layers with conviction and measured priorities. If it's bad, you've saved yourself from building infrastructure for a hypothetical customer.

Per the README philosophy: that's the discovery-first move. The infra already works well enough to run the experiment.

## Related artifacts

- `secrets-model.md` — the full four-layer plan + threat model + implementation specs, so "when signal appears" doesn't mean "start from scratch."
- `competitive-landscape.md` — mechanics + positioning landscape, so future pitch-framing doesn't re-research.
- `red-team-2026-04-17.md` — empirical baseline for how agents behave under social engineering, so future hardening doesn't start blind.

## When to revisit this prescription

- Signal from (1)-(3) arrives — either positive (ship deferred layers) or null (reassess the product hypothesis entirely).
- A competitor makes a move that compresses the window (Manus expanding to public handles, Anthropic shipping a catalog, etc. — see `competitive-landscape.md` triggers).
- A real security incident (credential leak from an agent, successful social-engineering exfil) forces Layer 3 priority regardless of market signal.
- Two weeks elapse without signal and without explicit reassessment — that's the default "am I rationalizing?" checkpoint.
