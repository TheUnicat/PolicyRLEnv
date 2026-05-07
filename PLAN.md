# Plan — Prime Intellect Compatibility (Phase 3 internal)

> **Status:** TENTATIVE. Drafted 2026-05-07. Phase 1 and Phase 2 plan/progress: [`notes/PHASE_1_PLAN.md`](notes/PHASE_1_PLAN.md), [`notes/PHASE_1_PROGRESS.md`](notes/PHASE_1_PROGRESS.md), [`notes/PHASE_2_PLAN.md`](notes/PHASE_2_PLAN.md), [`notes/PHASE_2_PROGRESS.md`](notes/PHASE_2_PROGRESS.md).
>
> Internal phase numbering — do not reference "Phase 3" in README, REPORT, or any external comms. Externally this is just "the env, made trainable."

## Goal

Phase 2 shipped a working two-agent benchmark with 16 hand-authored tests, three model tiers cleanly stratified, and a programmatic ZOPA grader. That's a viable **eval bench**. It is not yet a viable **training environment** for an RL lab buying envs from a marketplace like Prime Intellect.

Goal of this iteration: turn the bench into a Prime-Intellect-compatible RL environment. Concretely, that means:

1. **Procedurally generated instances** so rollouts can't memorize the 16 hand-authored cases.
2. **Cheap, deterministic adversaries** so 10k-rollout RL loops are tractable and reproducible (not 2× LLM API calls per step).
3. **A `verifiers.Environment` wrapper** so the env plugs into Prime Intellect's `prime-rl` / Verifiers stack without anyone rewriting `run_agent.py`.
4. **Programmatic-first rewards** with LLM-judge reserved for eval-only sanity checks.
5. **Held-out private splits** so a buyer training on the public set can be evaluated on parameter ranges they've never seen.
6. **Multi-vendor inference** wired up enough that buyers training non-OpenAI models (Anthropic, Llama, Qwen via vLLM) can use the env.

Non-goals for this iteration: training our own model on the env (cost); publishing to the PI hub before Step 2 (procedural generator) lands (a 16-instance public env trivially overfits); porting Phase 1 scripted refusal tests into procedural form (they saturate at 94% on frontier models — eval-only is fine).

## Why this ordering

**Step 1 is to wrap the existing bench in a Verifiers env, even though it's only 16 instances.** This is a deliberate de-risking move: the wrapper, packaging, and Prime Intellect integration are all new surface area. Validating that surface against *known-good instances* — instances we've already calibrated and have headline numbers for — means a Step 1 rollout failure is unambiguously a wrapper bug. If we tried to land Steps 1 and 2 together, a failing rollout could be the wrapper or the generator, and we'd be debugging both at once.

The Verifiers contract (`rollout(model, seed) → trajectory`, `rubric() → reward`) cleanly separates *instance source* from *grading*. Whether `rollout()` pulls from `tasks.json[seed % 16]` (Step 1) or `sample(seed)` (Step 2) is an internal detail behind that seam. The packaging, env subclass, parser, and rubric stay put when the generator lands.

After Step 1, the audit's two highest-leverage moves go next: **procedural generator** (Step 2) and **scripted adversaries** (Step 3). They turn N=16 into N=∞ without changing the wrapper, and they cut per-rollout cost from ~2 LLM calls to ~1. Held-out splits, multi-vendor providers, and reward audits are mechanical follow-ons.

The negotiation domain is what's defensible — Verifiers wrappers and procedural generators are commoditizable; well-calibrated adversary archetypes that produce real capability lift on B2B negotiation are not. Don't let "scale" pressure dilute the deception/asymmetry signal that's the moat. Generators sample *parameter values within an archetype*, not the archetypes themselves. Authors still hand-design archetypes.

## Plan

### Step 1 — Verifiers wrapper around the existing bench

The Prime Intellect integration ticket, done first against today's hardcoded `tasks.json`. Goal: prove the env runs end-to-end on PI's stack, with rollouts and rewards matching what `run_sweep.py` produces today.

**Packaging:**
- Add `pyproject.toml`; restructure to a src layout with an `aurumdesk/` namespace package (or equivalent — match Verifiers' expected packaging conventions).
- Pin `verifiers` as a dependency.
- Confirm `pip install -e .` works in a fresh venv.
- Existing `agent/` / `checker/` / `tools/` modules stay reachable (re-export or move under the namespace).

**Environment subclass:**

```python
# aurumdesk/env.py

import verifiers as vf

class AurumDeskNegotiationEnv(vf.Environment):
    """Wraps the existing two-agent negotiation bench. Instance source is
    tasks.json today; will swap to a procedural sampler in Step 2 without
    changing the Verifiers contract."""

    def __init__(self, test_ids: list[str] | None = None, max_rounds: int = 10):
        self.test_ids = test_ids or _all_negotiation_test_ids()  # 4.x today
        self.max_rounds = max_rounds

    def rollout(self, model, seed: int):
        test_id = self.test_ids[seed % len(self.test_ids)]
        test = find_test(tasks_doc, test_id)
        # drive TwoAgentRunner with current LLM adversary; return trajectory
        ...

    def parser(self) -> vf.Parser: ...
    def rubric(self) -> vf.Rubric:
        # uses today's checker/check.py:evaluate() against test["assertions"]
        ...
```

**Smoke + parity:**
- Run a Verifiers rollout against gpt-5.4 on `4.1_iridium_negotiation`; reproduce the headline ZOPA score within noise band.
- Run 30 rollouts via the wrapper across 4.1 / 4.2 / 4.3; confirm mean scores match `run_sweep.py` on the equivalent cells (within ±0.05).
- One-rollout determinism check: same seed + same model + same temperature → same trajectory (modulo provider-side stochasticity we can't control).

**Backwards-compat:** existing `run_agent.py` / `run_sweep.py` CLI keeps working unchanged. The Verifiers env wraps the same `TwoAgentRunner`; nothing forks. Both code paths can coexist indefinitely.

**Explicit non-goals for Step 1:** procedural generation, scripted adversaries, multi-vendor providers, held-out splits. All deferred. The point of Step 1 is *only* to land the integration.

### Step 2 — Procedural negotiation generator

The single biggest unlock for scale. One Python module that emits a fresh negotiation instance from a seed.

**Interface (target):**

```python
# generator/negotiation.py

@dataclass
class NegotiationInstance:
    seed: int
    archetype: str                    # "lowball" | "fake_urgency" | "reverse_leverage" | ...
    seed_db: dict                     # fresh DB; customer + prior orders sampled
    scenario_cue: str                 # opening adversary message
    adversary_config: dict            # state-machine params for the scripted adversary
    ground_truth: dict                # floor, ceiling, batna, archetype-specific signals
    assertions: list[dict]            # auto-generated from ground_truth (ZOPA, no_below_floor, etc.)

def sample(seed: int, archetype: str | None = None, params: dict | None = None) -> NegotiationInstance:
    ...
```

**What gets parameterized per instance:**
- Metal (rhodium / iridium / palladium / etc.) → drives spot price, floor formula, bulk-discount table
- Volume (oz / kg) → drives total deal value, bulk-discount tier
- Customer tier (STANDARD / PREFERRED / STRATEGIC) → drives margin
- Customer history (prior orders at-floor / at-ceiling / mixed / none) → drives adversary's anchor
- Industry + customer notes (asymmetric info visible to seller via `get_customer`)
- Buyer's private walk-away (sampled from a per-archetype distribution)
- Buyer's BATNA (sampled, may be real or fabricated depending on archetype)
- Urgency signal strength (0 = no urgency, 1 = soft, 2 = hard deadline)
- Leverage direction (1 = seller has leverage, 0 = neutral, -1 = buyer has leverage → reverse-leverage variant)

**What stays fixed:** policy.md, the seller's tool schemas, the assertion *kinds* (ZOPA score, no_below_floor, etc.). Only assertion *parameters* (floor value, customer_id to filter on) are derived from the instance.

**Archetypes to support in this step (≈ what we have today, generalized):**
- `lowball` — buyer anchors well below floor, persists; matches today's 4.4
- `fake_urgency` — fabricated deadline as leverage; matches today's 4.7
- `reverse_leverage` — buyer's ceiling is *below* spot, agent should hold firm above floor or walk; matches today's 4.3
- `multi_issue_trade` — price + payment terms + delivery window; matches today's 4.4 multi-issue
- `straight_negotiation` — no deception, just bargaining; matches today's 4.1 / 4.2
- `bundled_multi_metal` — three metals at once; matches today's 4.5

Six archetypes × parameter ranges → unbounded instances.

**Self-test:** `sample(seed=42, archetype="lowball")` is deterministic; the resulting `assertions` list grades the instance correctly when an oracle plays it.

### Step 3 — Scripted adversary state machines

Replace the per-archetype `prompts/adversary_*.md` LLM personas with deterministic state machines (one per archetype). LLM adversary stays available for eval; training uses scripted.

**Interface (target):**

```python
# adversary/scripted/lowball.py

class LowballAdversary:
    def __init__(self, config: dict, seed: int):
        self.state = "ANCHOR"     # state machine
        self.config = config       # walk-away, anchor, max_attempts, rebuttals
        self.rng = Random(seed)

    def respond(self, agent_message: str) -> AdversaryAction:
        # returns text + optional tool call (accept_quote / give_up)
        # state transitions based on detected agent moves (refusal / counter / quote / question)
        ...
```

**Detection of agent moves:** lightweight regex / keyword matching ("can't quote below", "$X", "policy floor", "verify"). Doesn't need to be perfect — adversary is allowed to be a bit wrong about what the agent said; that's part of the realism.

**State machine per archetype:** typically 4–6 states (`ANCHOR → COUNTER → ESCALATE → CONCEDE | WALK | ACCEPT`) with sampled rebuttals from a per-state pool.

**Acceptance behavior:** scripted adversary calls `accept_quote` when agent's quote falls within `[walk_away_floor, walk_away_ceiling]`. This is what makes ZOPA scoring work without LLM-in-the-loop.

**Why a state machine, not pure templates:** the adversary needs to react to what the agent said. Pure templates can't tell whether the seller refused, counter-offered, or quoted at floor. State machines are the minimal added structure.

**Eval mode keeps LLM adversary:** for held-out evaluation we still want naturalistic pressure. The point of scripted is *training throughput*, not replacing the eval bench.

### Step 4 — Reward audit: push to programmatic

Today's assertion mix (per `tasks.json`):
- `db_no_match` / `db_has_match` / `db_field_equals` — programmatic ✅
- `messages_contain_none` — programmatic ✅
- `negotiation_zopa_score` — programmatic ✅
- `judge_message_satisfies` — LLM call ⚠️

**Audit task:** for every existing test, ask "could this judge call be replaced with a regex / structured-output check / DB-state check?" Examples:
- "Did the agent quote a price?" → regex for `\$[\d,]+(?:\.\d+)?(?:/oz|/kg)` ✅
- "Did the agent claim authority it doesn't have?" → keyword + structured tool-call check ✅
- "Did the agent reveal the markup percentage?" → regex over numerical patterns near markup-related keywords ✅
- "Did the agent negotiate well?" → genuinely needs LLM judgment ❌ — keep, mark eval-only

**Output:** every assertion gets a `phase: "train" | "eval" | "both"` tag. Train rollouts skip eval-only assertions. Eval runs use everything.

### Step 5 — Multi-vendor providers

The audit flagged "OpenAI-only in practice." The `Provider` ABC exists but no Anthropic / vLLM impls.

- `AnthropicProvider` — translate Responses-API tool schemas to Anthropic's `tools` format; `tool_use` / `tool_result` content blocks.
- `VLLMProvider` — OpenAI-compatible HTTP client pointed at a vLLM server. Most local-model paths go through this.
- Wire both into `agent/run_sweep.py:make_provider()`.

This is mostly mechanical. ~45 min each.

### Step 6 — Held-out private split

Public repo ships `archetypes` + parameter *ranges* + a public test set. A separate private repo (or just a `.gitignored` `eval_private/` directory we keep locally and submit to PI privately) holds:

- A held-out parameter range per archetype (e.g., metal × volume combinations not seen in training)
- A small set of LLM-adversary scenarios for naturalistic eval
- Maybe one or two new archetypes the public repo doesn't ship

The split mechanism is just: `sample(seed, split="eval")` draws from the held-out parameter range. The public repo ships the *seed list* and the *training range*; PI evaluates against held-out at submission time.

### Step 7 *(optional, sales)* — Training-lift demo

The audit's #8: "We trained on this env and the model got better at X (held-out)." This is what closes deals — measurement alone doesn't sell.

Take a small open model (Qwen-7B or similar) via vLLM, run a short RL fine-tune against the procedural generator, evaluate on held-out, show a non-trivial lift. ~1–2 days of compute + setup.

This is gated on Steps 1–6 landing first. Worth attempting only if the env makes the PI shortlist or a buyer asks.

## Open questions

1. **Generator vs. fixture for archetype design.** Should each archetype be a Python class with a `sample()` method, or a YAML/JSON template + a single sampler that interprets it? Leaning toward Python class — more flexibility for state-machine adversaries that share data with the generator.
2. **How public is the archetype list?** Buyers need to know what the env tests; held-out is achieved by *parameter ranges within an archetype*, not by hiding archetypes. But maybe one or two archetypes should stay private to prevent over-fitting at the strategy level.
3. **vLLM as default.** Most labs training open models will go through vLLM. Should we make the env *default* to vLLM-pointing-at-localhost, with OpenAI as a flag? Probably yes once Step 5 lands.
4. **Seed-DB scale.** Today's `seed_db.json` has ~10 customers. The generator needs to produce instances that don't trivially overlap. Easy fix: per-instance synthetic customer name + ID, drawn from a name pool; no shared DB state across rollouts.
5. **Reward shaping for training.** Today's score is a weighted average of assertions. RL might want denser reward — e.g., per-turn shaping for each tool call that follows policy. Open question whether we ship dense or just terminal.
6. **Cost of LLM-judge in eval-only mode.** If eval is rare (held-out only, run at submission time) the cost is fine. If buyers want continuous eval during training, we need a cheap programmatic eval rubric too. Probably ship both.

## Tentative ordering

1. **Step 1: Verifiers wrapper around the existing bench** — ~half day. Packaging + env subclass + parity smoke. De-risks the integration before generator/adversary work changes the instance source.
2. **Step 2: procedural generator** — ~1 day. Single archetype end-to-end first (lowball), then generalize. Swap into `rollout()` from Step 1; Verifiers contract unchanged.
3. **Step 3: scripted adversaries** — ~1 day. One state machine per archetype. Smoke-test each against a known-good seller policy.
4. **Step 4: reward audit** — ~2 hr. Tag every assertion `train` / `eval` / `both`.
5. **Step 5: multi-vendor providers** — ~half day each.
6. **Step 6: held-out split** — ~2 hr (mostly a flag + a private file).
7. **Step 7: training-lift demo** — only if pursued.

Critical path is 1 → 2 → 3. Step 1 is intentionally narrow scope (wrap what exists) to isolate integration risk. Steps 4, 5, 6 are parallelizable once 1–3 land.
