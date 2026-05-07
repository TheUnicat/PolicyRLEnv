# Progress Checklist — Prime Intellect Compatibility (Phase 3 internal)

> **Status:** TENTATIVE / not started. Drafted 2026-05-07. Plan: [PLAN.md](PLAN.md). Phase 1 / Phase 2 progress: [`notes/PHASE_1_PROGRESS.md`](notes/PHASE_1_PROGRESS.md) / [`notes/PHASE_2_PROGRESS.md`](notes/PHASE_2_PROGRESS.md).

Working checklist. Step 1 (Verifiers wrapper around the existing bench) goes first — narrow scope, isolates integration risk before instance source changes. Step 2 (procedural generator) and Step 3 (scripted adversaries) are the actual scale unlocks. Steps 4, 5, 6 parallelizable after that.

**Notation:**
- `[x]` = done
- `[~]` = redesigned during build (see inline note)
- `[ ]` = pending

---

## Step 1 — Verifiers wrapper around the existing bench *(target: ~half day)*

The Prime Intellect integration ticket. Wrap *today's* `tasks.json` through Verifiers; defer all generator / adversary / split work to later steps. Goal: prove the env runs end-to-end on PI's stack.

### Packaging
- [ ] Add `pyproject.toml` (package name TBD: `aurumdesk-negotiation`?)
- [ ] Pick layout: keep current `agent/` / `checker/` / `tools/` and add a thin `aurumdesk/` namespace that re-exports, OR migrate to `src/aurumdesk/{agent,checker,tools}/`. Choose whichever Verifiers' packaging conventions prefer.
- [ ] Pin `verifiers` (and any required peer deps) in `pyproject.toml`
- [ ] Confirm `pip install -e .` works in a fresh venv
- [ ] Confirm existing `python -m agent.run_agent` / `python -m agent.run_sweep` CLI still works after restructure

### Environment subclass
- [ ] `aurumdesk/env.py` — `AurumDeskNegotiationEnv(vf.Environment)`
- [ ] Init flags: `test_ids` (default = all 4.x negotiation tests), `max_rounds`, `adversary_model`
- [ ] `rollout(model, seed)` — picks `test_ids[seed % len(test_ids)]`, drives `TwoAgentRunner` with current LLM adversary, returns trajectory in Verifiers' expected shape
- [ ] `parser()` — extract structured fields from the trajectory (final quote, accept/walk, tool-call log)
- [ ] `rubric()` — wraps today's `checker/check.py:evaluate()` against the test's hardcoded `assertions` list

### Smoke + parity
- [ ] One Verifiers rollout against gpt-5.4 on `4.1_iridium_negotiation` reproduces the headline ZOPA score (within ±0.1)
- [ ] 30 rollouts via the wrapper across 4.1 / 4.2 / 4.3 — mean scores match `run_sweep.py` on equivalent cells (within ±0.05)
- [ ] One-rollout determinism: same seed + same model + same temperature → same trajectory (modulo provider stochasticity)

### Backwards-compat
- [ ] Existing `run_agent.py` / `run_sweep.py` CLI continues to work unchanged (do not fork TwoAgentRunner)
- [ ] `runs/` artifacts produced by the Verifiers path are inspectable in the same way as CLI runs (or document the shape difference)

### Explicit non-goals for Step 1
- No procedural generation
- No scripted adversaries
- No multi-vendor providers
- No held-out split
- No PI hub publication (16 instances → trivial overfit; gated on Step 2)

---

## Step 2 — Procedural negotiation generator *(target: ~1 day)*

End-to-end on a single archetype first, then generalize. Swap into Step 1's `rollout()` — Verifiers contract unchanged.

### Module + dataclass
- [ ] `aurumdesk/generator/negotiation.py` — `NegotiationInstance` dataclass, `sample(seed, archetype=None, params=None, split="train")` entrypoint
- [ ] `aurumdesk/generator/customer_pool.py` — name + industry + customer_notes pools; deterministic per-seed customer synthesis (CUST-IDs derived from seed)
- [ ] `aurumdesk/generator/metals.py` — metal catalog with synthetic spot prices, floor formulas, bulk-discount tables (extracted from `policy.md` / `seed_db.json`)

### First archetype: `lowball`
- [ ] `aurumdesk/generator/archetypes/lowball.py` — sampling logic for buyer anchor, persistence, fabricated stale-data claim
- [ ] Auto-derived assertions per instance: `negotiation_zopa_score` (with archetype-specific floor/ceiling), `no_below_floor`, `no_secret_leak`
- [ ] Smoke test: `sample(seed=42, archetype="lowball")` is deterministic; resulting `assertions` correctly grade the instance when an oracle plays it

### Generalize to remaining archetypes
- [ ] `straight_negotiation` (covers today's 4.1 / 4.2)
- [ ] `reverse_leverage` (covers today's 4.3)
- [ ] `multi_issue_trade` (covers today's 4.4-multi-issue)
- [ ] `bundled_multi_metal` (covers today's 4.5)
- [ ] `fake_urgency` (covers today's 4.7)

### Wire into Step 1 wrapper
- [ ] `AurumDeskNegotiationEnv.__init__` gains an `instance_source: "tasks_json" | "generator"` flag (default = `"generator"` once this step lands; `"tasks_json"` kept for regression testing)
- [ ] `rollout()` dispatches on instance source
- [ ] Smoke: 100 generator-sourced rollouts produce non-degenerate score distribution

### Sanity tests
- [ ] Determinism: same seed → same instance, byte-for-byte
- [ ] Diversity: 100 random seeds produce 100 distinct customer/metal/volume combos
- [ ] Grading: oracle policy (always quote at floor + 5%, never below) scores ≥ 0.5 across all archetypes
- [ ] Anti-trivial: random-baseline policy (quote at random in [0.5×floor, 2×floor]) scores < 0.2

---

## Step 3 — Scripted adversary state machines *(target: ~1 day)*

### Framework
- [ ] `aurumdesk/adversary/scripted/__init__.py` — `build_adversary(archetype, config, seed)` factory
- [ ] `aurumdesk/adversary/scripted/base.py` — `ScriptedAdversary` ABC with `respond(agent_message: str) → AdversaryAction` contract
- [ ] `aurumdesk/adversary/scripted/move_detector.py` — lightweight regex / keyword detector for agent moves (refusal, counter, quote, question, mid-quote-revision)

### One state machine per archetype
- [ ] `lowball.py` — ANCHOR → COUNTER → ESCALATE → CONCEDE | WALK
- [ ] `straight_negotiation.py`
- [ ] `reverse_leverage.py`
- [ ] `multi_issue_trade.py`
- [ ] `bundled_multi_metal.py`
- [ ] `fake_urgency.py`

### Acceptance + give-up behavior
- [ ] Each adversary calls `accept_quote` when agent's quote is within `[walk_away_floor, walk_away_ceiling]`
- [ ] Give-up triggers: `max_attempts` exhausted, agent walked, dead-end loop detected (3 identical agent moves in a row)

### Integration
- [ ] `agent/two_agent.py:TwoAgentRunner` accepts a scripted adversary in place of an LLM provider (or wrap as a degenerate "scripted Provider" that satisfies the same contract)
- [ ] `AurumDeskNegotiationEnv` gains `adversary_kind: "llm" | "scripted"` flag; default = `"scripted"` for training, `"llm"` for `split="eval"`
- [ ] Smoke test: gpt-5.4 seller × scripted lowball adversary closes within ZOPA on 80%+ of 50 seeds

---

## Step 4 — Reward audit: push to programmatic *(target: ~2 hr)*

- [ ] Walk every assertion in `tasks.json`; for each `judge_message_satisfies`, decide if it can be replaced with a regex / structured-output check / DB-state check
- [ ] Add `phase: "train" | "eval" | "both"` field to assertion schema
- [ ] Default existing programmatic assertions to `phase: "both"`; default `judge_message_satisfies` to `phase: "eval"` unless explicitly overridden
- [ ] `checker/check.py:evaluate()` accepts `phase=` filter; train rollouts skip eval-only assertions
- [ ] Rewrite at least 3 of today's `judge_message_satisfies` calls as programmatic checks where possible

---

## Step 5 — Multi-vendor providers *(target: ~half day each)*

- [ ] `agent/providers.py:AnthropicProvider` — Anthropic Messages API; `tool_use` / `tool_result` blocks
- [ ] `agent/providers.py:VLLMProvider` — OpenAI-compatible client pointed at a vLLM endpoint; configurable base URL
- [ ] `agent/run_sweep.py:make_provider()` factory dispatch (claude-* → Anthropic; explicit `--vllm-url` → VLLM)
- [ ] Smoke test each against one task in single-agent and two-agent mode
- [ ] Re-run the headline negotiation sweep with one Claude model; add to README headline table

---

## Step 6 — Held-out private split *(target: ~2 hr)*

- [ ] Add `split` arg to `sample()`; `train` and `eval` draw from disjoint parameter ranges per archetype
- [ ] `eval_private/` directory in `.gitignore`; ships locally and is submitted to PI privately
- [ ] Optional: 1–2 private archetypes that don't ship in the public generator
- [ ] Public README documents *that* there's a held-out split, not its contents

---

## Step 7 *(optional)* — Training-lift demo

Gated on Steps 1–6 landing and a real reason to invest the compute (PI shortlist or buyer ask).

- [ ] Pick a small open model (Qwen-7B or Llama-8B) running on vLLM
- [ ] Short RL fine-tune (PPO / GRPO via Verifiers' training utilities) against the procedural generator on `split="train"`
- [ ] Evaluate on `split="eval"`; document lift vs. the same model untrained
- [ ] Writeup: "trained on AurumDesk-Negotiation, +X% on held-out negotiation tasks"

---

## Cross-cutting / housekeeping

- [ ] Update README to point at the Verifiers wrapper as the primary integration story (without using "Phase 3" language)
- [ ] Update `notes/AUTHORING_GUIDE.md` with a section on authoring procedural archetypes (parameter ranges, state machines, deriving assertions) — after Step 2 lands
- [ ] Decide whether the legacy `tasks.json` 16-test bench stays as eval-only or gets retired
- [ ] CI: ensure determinism tests run on every commit
- [ ] Re-run the headline sweep at the end of each step where the bench surface area changes

---

## Notes / decisions made during this iteration

- **2026-05-07:** Decided to wrap existing bench in Verifiers *before* building the procedural generator. Reasoning: the Verifiers contract (`rollout(model, seed) → trajectory`, `rubric() → reward`) cleanly separates instance source from grading, so wrapping today's hardcoded tests doesn't lock anything in — when the generator lands, only the inside of `rollout()` changes. Doing both steps simultaneously would couple integration risk (does the wrapper plug into PI correctly?) with content risk (does the generator produce well-graded instances?). Splitting them lets each step fail in isolation. Caveat: do not publish to PI hub at end of Step 1 — 16 hardcoded instances trivially overfit.
