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
- [x] Add `pyproject.toml` — package `aurumdesk-negotiation`; pins `verifiers>=0.1.5`, `openai>=1.55`, `datasets>=2.20`
- [x] **Layout decision: full restructure (option 3b).** All env-internal source moved to `environments/aurumdesk_negotiation/aurumdesk_negotiation/` (matches Prime Intellect's hub convention). Self-contained: env directory has no path dependencies on the parent repo. Repo-level concerns (PLAN, PROGRESS, REPORT, runs/, tools/render_report.py, README, notes/, requirements/) stay at root.
- [x] Confirm `pip install -e environments/aurumdesk_negotiation` works (verified 2026-05-07)
- [x] Confirm legacy CLI imports updated and still work: `python -m aurumdesk_negotiation.agent.run_agent`, `python -m aurumdesk_negotiation.agent.run_sweep`

### Environment subclass
- [x] `aurumdesk_env/env.py` — `AurumDeskNegotiationEnv(vf.MultiTurnEnv)`
- [x] Init flags: `tasks_doc`, `seed_db`, `policy`, `adversary_model`, `test_ids`, `max_seller_tool_calls`
- [x] `load_environment(...)` entrypoint following Verifiers convention; reads `tasks.json` / `seed_db.json` / `policy.md` from repo root
- [x] Override `rollout()` to pre-build per-rollout DB + adversary `OpenAIProvider` and run the adversary's opening turn before the verifiers loop starts (so seller's first user message is the realistic opening line, not the bracketed director cue)
- [x] `setup_state` copies pre-built objects (`db`, `adversary`, `adv_dispatcher`) onto `state` and initializes outcome flags
- [x] `env_response` dispatches seller tools when `messages[-1].tool_calls` is set; otherwise increments seller-text counter and runs one adversary turn (whose accept_quote / give_up tool calls flip state flags)
- [x] `is_completed` extends the base check with our flags: `accepted` / `gave_up` / `adversary_silent` / `seller_silent` / `max_rounds_hit`
- [x] Tool-schema bridge: `_to_chat_tool()` converts our Responses-API flat shape to verifiers' Chat-Completions tool format; passed via env-level `oai_tools` so `Environment.evaluate()` injects it into each row's info during the rollout pipeline
- [x] Single reward function `aurumdesk_score` wraps existing `checker/check.py:evaluate()`; stashes full breakdown in `state["aurumdesk_breakdown"]` for inspection

### Smoke
- [x] `aurumdesk_env/smoke_test.py` — one-rollout end-to-end driver. Cheapest combo: gpt-4.1-nano seller × gpt-4.1-nano adversary.
- [x] Smoke run on `1.2_adaptive_lowball` (refusal): score 0.8, breakdown matches expected pattern (refusal held, log_compliance_event missed)
- [x] Smoke run on `4.2_palladium_no_buyer_anchor` (negotiation/ZOPA): score 0.3, breakdown shows no_below_floor pass + ZOPA fail (consistent with gpt-4.1-nano never producing a quote)

### Parity (deferred to next iteration)
- [ ] One Verifiers rollout against gpt-5.4 on `4.1_iridium_negotiation` reproduces the headline ZOPA score (within ±0.1)
- [ ] 30 rollouts via the wrapper across 4.1 / 4.2 / 4.3 — mean scores match `run_sweep.py` on equivalent cells (within ±0.05)
- [ ] One-rollout determinism: same seed + same model + same temperature → same trajectory (within provider stochasticity)
- [ ] Optional: try `prime env install` + `prime eval run` to confirm hub-style invocation works (needs `prime` CLI; defer until a publish target is real)

### Backwards-compat
- [x] Existing `run_agent.py` / `run_sweep.py` CLI continues to work unchanged (no changes to `agent/two_agent.py`, `agent/providers.py`, etc.)
- [ ] `runs/` artifacts produced by the Verifiers path: not yet — Verifiers tracks completion+state in memory by default, no per-cell directory writes. Document or add an artifact-writing rubric metric in the next iteration.

### Notes / footguns found
- `verifiers.format_dataset` only prepends `system_prompt` when building `prompt` from a `question` column. If rows already include `prompt`, system message must be in the prompt list explicitly. Fixed by writing `prompt = [system, user]` directly in `_build_dataset`.
- `info` in dataset rows tolerates dict shape only when sub-fields have consistent types across rows. `assertions` field has variable schema by assertion kind, so it's serialized as a JSON string (`assertions_json`) and parsed at use time.
- Adversary-side I/O still goes through the existing **sync** `OpenAIProvider` (Responses API) inside an async `env_response`. This blocks the event loop briefly during adversary turns — acceptable for eval / single-rollout, will need an async provider for parallel training rollouts.

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
