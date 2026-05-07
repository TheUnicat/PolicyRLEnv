"""
Run a single AurumDesk benchmark test against a single model.

Auto-detects mode from the test schema:
    - presence of `user_messages` → scripted mode (existing behavior)
    - presence of `adversary_prompt_file` → two-agent mode

Usage (scripted):
    python -m agent.run_agent --test 1.1_persistent_lowball --model gpt-4.1-nano

Usage (two-agent):
    python -m agent.run_agent --test 1_adaptive_lowball --model gpt-5.4
    python -m agent.run_agent --test 1_adaptive_lowball --model gpt-4.1-mini --adversary-model gpt-5.4
    python -m agent.run_agent --test 1_adaptive_lowball --model gpt-5.4 --max-rounds 8

Reads OPENAI_API_KEY from the environment. Writes outputs under
runs/<model>/<test_id>/run_<run_id>/. Common files:
    trace.jsonl, final_db.json, messages.json, score.json, turn_summaries.json
Two-agent mode also writes:
    adversary_messages.json, outcome.json
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from . import tools as agent_tools
from .adversary_tools import ADVERSARY_TOOL_SCHEMAS, make_adversary_dispatcher
from .models import AgentRunner
from .providers import OpenAIProvider
from .two_agent import TwoAgentRunner
from ..checker.check import evaluate, find_test


PKG_DIR = Path(__file__).resolve().parent.parent

DEFAULT_SCENARIO_CUE = "[Begin the conversation. You speak first.]"


def _safe(name: str) -> str:
    return name.replace("/", "_").replace(":", "_")


def _detect_mode(test: dict) -> str:
    has_scripted = "user_messages" in test
    has_two_agent = "adversary_prompt_file" in test or test.get("mode") == "two_agent"
    if has_scripted and has_two_agent:
        raise SystemExit(f"test {test.get('test_id')} declares both user_messages and adversary_prompt_file — pick one")
    if has_scripted:
        return "scripted"
    if has_two_agent:
        return "two_agent"
    raise SystemExit(f"test {test.get('test_id')} declares neither user_messages nor adversary_prompt_file")


# ---------- Scripted mode ----------

def run_scripted(test: dict, args, policy: str, db: dict, out_dir: Path) -> dict:
    provider = OpenAIProvider(
        model=args.model,
        system_prompt=policy,
        tools=agent_tools.TOOL_SCHEMAS,
        max_tool_calls_per_turn=args.max_tool_calls,
    )
    runner = AgentRunner(provider, db)

    n_turns = len(test["user_messages"])
    for i, msg in enumerate(test["user_messages"]):
        print(f"--- turn {i + 1}/{n_turns} ---")
        result = runner.run_one_message(msg)
        print(f"  status={result.status}, tool_calls={result.tool_calls_used}")
        if runner.assistant_messages:
            tail = runner.assistant_messages[-1]
            print(f"  last assistant msg ({len(tail)} chars): {tail[:120]!r}{'...' if len(tail) > 120 else ''}")
        if result.status == "error":
            break

    score = evaluate(test["assertions"], db, runner.assistant_messages)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trace.jsonl").write_text("\n".join(json.dumps(r, default=str) for r in runner.trace) + "\n")
    (out_dir / "final_db.json").write_text(json.dumps(db, indent=2))
    (out_dir / "messages.json").write_text(json.dumps(runner.assistant_messages, indent=2))
    (out_dir / "score.json").write_text(json.dumps(score, indent=2))
    (out_dir / "turn_summaries.json").write_text(json.dumps(runner.turn_summaries, indent=2))
    return score


# ---------- Two-agent mode ----------

def run_two_agent(test: dict, args, policy: str, db: dict, out_dir: Path) -> dict:
    adv_prompt_path = PKG_DIR / test["adversary_prompt_file"]
    if not adv_prompt_path.exists():
        raise SystemExit(f"adversary_prompt_file not found: {adv_prompt_path}")
    adv_prompt = adv_prompt_path.read_text()

    # Optional shared preamble (e.g., the deceptive-eval-context). Used for
    # tasks where the buyer is doing something a model might refuse on safety
    # grounds; not used for normal-negotiation tasks where the buyer is just
    # hard-bargaining honestly.
    prepend_path_rel = test.get("adversary_prompt_prepend")
    if prepend_path_rel:
        prepend_path = PKG_DIR / prepend_path_rel
        if not prepend_path.exists():
            raise SystemExit(f"adversary_prompt_prepend not found: {prepend_path}")
        adv_prompt = prepend_path.read_text() + adv_prompt

    scenario_cue = test.get("scenario_cue", DEFAULT_SCENARIO_CUE)
    max_rounds = args.max_rounds if args.max_rounds is not None else int(test.get("max_rounds", 6))
    adv_model = args.adversary_model or args.model

    agent_provider = OpenAIProvider(
        model=args.model,
        system_prompt=policy,
        tools=agent_tools.TOOL_SCHEMAS,
        max_tool_calls_per_turn=args.max_tool_calls,
    )
    adv_provider = OpenAIProvider(
        model=adv_model,
        system_prompt=adv_prompt,
        tools=ADVERSARY_TOOL_SCHEMAS,
        max_tool_calls_per_turn=3,  # buyer may call accept_quote + give_up in one turn; small buffer
    )

    def agent_dispatch(name: str, dargs: dict) -> dict:
        return agent_tools.dispatch(db, name, dargs)

    runner = TwoAgentRunner(
        agent_provider=agent_provider,
        agent_dispatcher=agent_dispatch,
        adversary_provider=adv_provider,
        adversary_dispatcher=make_adversary_dispatcher(db=db),  # gives buyer access to accept_quote
        scenario_cue=scenario_cue,
        max_rounds=max_rounds,
    )

    print(f"--- two-agent mode: agent={args.model}, adversary={adv_model}, max_rounds={max_rounds} ---")
    result = runner.run()
    print(f"  outcome={result.outcome}")
    print(f"  agent turns: {sum(1 for s in result.turn_summaries if s['side'] == 'agent')}, "
          f"adversary turns: {sum(1 for s in result.turn_summaries if s['side'] == 'adversary')}")
    if result.agent_messages:
        tail = result.agent_messages[-1]
        print(f"  last agent msg ({len(tail)} chars): {tail[:120]!r}{'...' if len(tail) > 120 else ''}")

    # Eval lives outside the runner — runner just produced the artifacts.
    score = evaluate(test["assertions"], db, result.agent_messages)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trace.jsonl").write_text("\n".join(json.dumps(r, default=str) for r in result.trace) + "\n")
    (out_dir / "final_db.json").write_text(json.dumps(db, indent=2))
    (out_dir / "messages.json").write_text(json.dumps(result.agent_messages, indent=2))
    (out_dir / "adversary_messages.json").write_text(json.dumps(result.adversary_messages, indent=2))
    (out_dir / "score.json").write_text(json.dumps(score, indent=2))
    (out_dir / "turn_summaries.json").write_text(json.dumps(result.turn_summaries, indent=2))
    (out_dir / "outcome.json").write_text(json.dumps({
        "outcome": result.outcome,
        "outcome_detail": result.outcome_detail,
        "agent_model": args.model,
        "adversary_model": adv_model,
        "max_rounds": max_rounds,
    }, indent=2))
    return score


# ---------- CLI ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", required=True, help="test_id from tasks.json")
    ap.add_argument("--model", required=True, help="OpenAI model name for the AurumDesk agent (e.g., gpt-4.1-nano, gpt-4.1-mini, gpt-5.4)")
    ap.add_argument("--adversary-model", default=None, help="OpenAI model for the adversary (two-agent mode only). Defaults to --model.")
    ap.add_argument("--run-id", default="1")
    ap.add_argument("--max-tool-calls", type=int, default=10, help="per-user-message tool-call cap (agent side)")
    ap.add_argument("--max-rounds", type=int, default=None, help="two-agent only: override per-test max_rounds")
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--tasks-file", default="tasks.json")
    ap.add_argument("--seed-db", default="seed_db.json")
    ap.add_argument("--policy", default="policy.md")
    args = ap.parse_args()

    tasks_doc = json.loads((PKG_DIR / args.tasks_file).read_text())
    test = find_test(tasks_doc, args.test)
    if test is None:
        raise SystemExit(f"test_id not found in {args.tasks_file}: {args.test}")

    seed = json.loads((PKG_DIR / args.seed_db).read_text())
    policy = (PKG_DIR / args.policy).read_text()
    db = copy.deepcopy(seed)

    # `out_dir` is cwd-relative — runs from the repo root land in ./runs/, runs
    # from elsewhere land relative to the user's cwd. Pass --out-dir for an absolute path.
    out_dir = Path(args.out_dir) / _safe(args.model) / args.test / f"run_{args.run_id}"

    mode = _detect_mode(test)
    if mode == "scripted":
        score = run_scripted(test, args, policy, db, out_dir)
    else:
        score = run_two_agent(test, args, policy, db, out_dir)

    print(f"\nscore: {score['score']} (passed={score['passed']})")
    for r in score["results"]:
        flag = "PASS" if r["passed"] else "FAIL"
        print(f"  [{flag}] w={r['weight']}  {r['kind']:24s}  {r['rationale'][:80]}")
    print(f"\noutput: {out_dir}")


if __name__ == "__main__":
    main()
