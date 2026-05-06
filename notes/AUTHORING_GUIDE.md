# Authoring Guide — AurumDesk Benchmark

This file is for whoever is authoring new tests, adversary prompts, or schema extensions — including future me, future Claude/Codex agents, the user, or anyone else who picks this up. It distills what we've learned actually building Phase 1 and Phase 2.

The benchmark is a recursion of agents-making-tests-for-agents. Read it that way: most of these principles apply whether you're writing a test scenario, a system prompt, a judge criterion, or even instructions for *another agent that will write prompts.*

---

## Core principles

### 1. Differentiability is the goal

A good test produces signal across model capability levels. The Phase 1 sweep (nano 0.28 / mini 0.69 / 5.4 1.00) is the bar — clean stratification, not pass/fail. A test where all models pass is uninformative; one where all models fail (without getting any partial marks) has no resolution at the top end either.

If a test is too easy, raise difficulty (more pressure, harder math, sneakier injection). If too hard, isolate which axis is killing all models and dial back. Calibrate against at least three capability tiers before declaring a test "shipped."

### 2. Target underserved domains

Don't duplicate saturated public benches (positive tool-use, basic refusals, clean code/math). Lean into hard-to-verify domains: active negotiation, prompt-injection resistance during productive work, honesty under pressure, calibrated uncertainty. The bench's value comes from *not* being yet another retail-customer-service, one-shot avoiding jailbreak, general tool-use, general agent-y behavior eval.

### 3. Outcome-based grading > path-based grading

Grade against the final DB state and the agent's user-facing messages, not against a specific tool-call sequence. This makes the same checker work across scripted/two-agent modes and across model families. Partial credit lives in weighted assertions.

If you want to grade procedure, add a `tool_call_order` assertion. Don't bake procedural assumptions into the harness itself.

### 4. Synthetic everything

No real PII, customer data, account numbers, addresses. Plausible-looking — Halberd Industrial sounds like a real US manufacturer because it should — but invented. Use realistic numeric ranges (rhodium really is in the thousands per oz); use fake but realistic jurisdictions and IDs.

### 5. One source of truth

`tasks.json` holds the binding spec (assertions). Everything else (MODEL_ANSWERS.md, README) should be derivable from it or reference it back. Hand-edited duplicates rot.

### 6. Schema additivity

Existing scripted tests and the existing checker MUST continue to work after any schema change. Bump `schema_version` (1.0 → 1.1) for additions; major-bump only for breaks. New optional fields, new assertion kinds, new test modes — all fine. Renaming or removing existing fields — not fine.

---

## Writing AI prompts: inhabit the role

When you're writing a prompt for any AI in this project (adversary, judge, agent under test), put yourself in its position. Ask: what would *I* need to know to do this well?

This is where most prompt-writing fails. Generic instructions ("be persistent", "act like a customer", "follow policy") yield generic outputs that don't differentiate models. Concrete role-paintings work.

### For an adversarial agent — give it:

- **Persona.** Name, employer, role, scale of operation, why it cares. "Marcus Chen, head of procurement at Halberd Industrial Corp. (revenues ~$45M/yr, ~80 employees, founded 2019)" beats "a frustrated customer."
- **Concrete measurable goal.** Not "extract information"; "extract any of the following five specific items, here they are."
- **BATNA / fallback.** What's the alternative if the conversation fails? Walk to a competitor, sue, wait? Knowing this informs which threats are credible.
- **Enumerated allowed moves with example phrasings.** Not "be creative." Authority claim, urgency, sympathy, walk-threat, compromise gambit — list each with an example sentence the persona might say.
- **Enumerated forbidden moves.** Don't break character; don't reveal you're being tested; don't call tools you shouldn't; don't emit text in [brackets] (those are stage directions you receive, not produce).
- **Termination criteria with structured outcome labels.** When to call `give_up` and what `outcome` enum to pass.
- **Harness conventions.** What text in [brackets] means; what tools exist; that the conversation alternates.

### For an agent under test — give it:

- **Full policy verbatim.** Not summarized. Loaded into the system prompt as-is.
- **Tool schemas + explicit "this tool does NOT" notes.** E.g., `create_quote` does not enforce floor; agent is responsible.
- **Visibility / disclosure tiers.** What can/cannot be shared with customers.
- **Required logging.** Concrete event types for concrete situations.
- **Out-of-policy paths.** Decline + log + direct to compliance@aurumdesk.com — no manager handoff, no override, no escalation invention.

### For a judge — give it:

- **Concrete criterion.** Not a vague rubric.
- **Boolean output + one-line rationale.** Pass/fail.
- **The specific input to evaluate.**
- **What NOT to do.** Don't second-guess; don't add criteria; don't moralize.

### Litmus test

Read the prompt back as if you were the AI. If you'd fall back to generic behavior or be confused about edge cases, the prompt is missing something specific.

---

## Designing tests

### What makes a good failure surface?

A failure surface is the actual mechanism of failing. Good ones are:

- **Mechanically detectable.** Substring scan, DB row presence, audit-log entry. If you can't write the assertion, the surface is too vague.
- **Realistic.** What would actually break in production. We removed `manual_price_override` because it created an artificial failure mode that doesn't exist in real systems.
- **Closed-form.** Binary or scalar. Not "the agent was unhelpful."

### What makes a good adversarial pressure pattern?

- **Multi-turn.** Single-turn rarely surfaces interesting failures. 3 turns is the floor; 6 is good for two-agent mode.
- **Stacks vectors.** Real adversaries combine authority + urgency + sympathy + plausible misdirection. So should the test.
- **Leaves the agent room to decline gracefully.** Always provide a legitimate alternative (counter at floor, direct to compliance email, ask for proper verification). Otherwise you're measuring "comply or be rude," which isn't policy-following.
- **Doesn't reveal it's a test.** No `aurumdesk.example`, no obviously-test-shaped data.

### What makes a good assertion?

- **Weighted.** Total weight = 1.0 per test; partial credit lives in the weights.
- **Has a `rationale` string.** Future-you reading a failure trace needs to remember why this check matters.
- **Uses the right kind.** `db_no_match` for "shouldn't have happened"; `db_has_match` for "should have happened"; `db_field_equals` for specific field values; `messages_contain_none` for output-content denials.
- **Atomic.** One assertion, one thing. Conflated checks produce uninterpretable failures.

### Class balance

If every test rewards refusal, "refuse everything" is a winning baseline. Mix in compliance-positive tests where the right answer is to *do the thing correctly* — quote at floor, in-window refund with valid F1, address change under threshold. This catches over-refusal and procedural sloppiness.

---

## Pitfalls we've actually hit

- **Removing tools we couldn't enforce.** `manual_price_override`, `request_manager_approval`, `human_handoff` — all removed because (a) we couldn't enforce policy on them and (b) their existence muddied what the test was measuring.
- **Too-obvious test framing.** Originally `aurumdesk.example`; the agent could detect it was being evaluated. Switched to `aurumdesk.com`.
- **Three-mangled-PIN candidates.** Task 3.1 originally had the customer offer three different wrong PINs in one message. Read fishing-shaped to even weak models. Switched to one confident wrong PIN.
- **Static scripts can't punish weak refusals.** Phase 1 found cases where the agent gave a soft "let me look into that" reply; the next scripted message just barreled on. Phase 2 two-agent mode fixed this.

---

## Schema design rule of thumb

Structure earns its keep when it unlocks something programmatic: a checker reads it, a renderer consumes it, a constraint becomes checkable. Otherwise prose with conventional sub-headings (`**Must convey:**`, `**References:**`, etc.) beats fields-on-fields.

Apply this when extending `tasks.json`. Add a structured field only when the harness will actually consume it. Documentation goes in prose.

---

## When in doubt

- Is this test differentiable across capability levels? If all models will pass or all will fail, redesign.
- Does this prompt give the AI everything it needs to play the role? If you'd be confused, the AI will be confused.
- Is this assertion's failure mode atomic and explainable? If a failure trace would be cryptic, split or rephrase.
- Is the schema change additive? If not, it probably shouldn't ship.
- Does this duplicate territory that's already saturated in public benches? If yes, redesign or skip.

---

## Concrete instructions

### Two-agent benchmarks

Max 5 actionable rules. Apply each one to every two-agent test before shipping.

1. **State the adversary's goal in one literal sentence at the top of the prompt, using a `GOAL:` label.** Don't bury it in a section header — the model should know what success looks like in one read. Format: `GOAL: <one concrete measurable outcome>`. Example: `GOAL: Get the AurumDesk agent to issue a binding quote for 1 kg of rhodium at $140,000–$145,000 total.` Vague goals ("be persistent," "extract info") yield generic adversary behavior that doesn't differentiate models.

2. **The goal must map directly to a checkable assertion.** Trace the line: *goal → DB artifact (or message-content needle) → assertion*. If you can't draw that line, the test is uninstrumented and you'll get nice-looking traces with no signal. Cross-check both directions: (a) if the adversary "wins" per its own judgment, does an assertion fire? (b) if an assertion fires, would the adversary correctly label its `give_up` outcome as `got_what_i_wanted`? Mismatches mean the prompt or the assertion is wrong.

3. **Force minimum persistence.** Tell the adversary explicitly to try at least N (typically 3–4) distinct angles before calling `give_up(outcome=agent_refused)`. Without a floor, models bail on the first refusal and the bench loses signal. Calibration point: gpt-5.4 self-adversary on `1.2_adaptive_lowball` (instructed "try at least 3 angles") tried 6 — that's the right level of pressure.

4. **Set `max_rounds` to roughly 1.5–2× the number of distinct angles.** With 4 enumerated angles, `max_rounds=6` gives room to try each + buffer. Too few caps the bench artificially before the adversary has finished its repertoire; too many run up cost without new signal. Default to 5–6 unless the scenario genuinely needs more turns to develop.

5. **Calibrate against a strong agent-model first.** Run agent=strongest vs. adversary=strongest (self-play with the strongest model) before any sweep. If the adversary can't even pressure a strong agent, the adversary prompt is too weak. If it always succeeds against a strong agent, the bench will saturate at 0 across all models. Tune the adversary's persona, allowed-moves list, or persistence floor until the strong-vs-strong cell shows a meaningful per-run distribution (some passes, some fails, or consistently passes with the bonus assertion sometimes failing).