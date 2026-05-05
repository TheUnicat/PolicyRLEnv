"""
Run a single AurumDesk benchmark test against a single model.

Usage:
    python -m agent.run_agent --test 1.1_persistent_lowball --model gpt-4.1-nano
    python -m agent.run_agent --test 2.1_jailbreak_plus_investment_dd --model gpt-5.4 --run-id 2

Reads OPENAI_API_KEY from the environment. Writes outputs under
runs/<model>/<test_id>/run_<run_id>/:
    trace.jsonl, final_db.json, messages.json, score.json, turn_summaries.json
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from agent import tools as agent_tools
from agent.models import AgentRunner
from agent.providers import OpenAIProvider
from checker.check import evaluate, find_test


REPO = Path(__file__).resolve().parent.parent


def _safe(name: str) -> str:
    return name.replace("/", "_").replace(":", "_")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", required=True, help="test_id from tasks.json")
    ap.add_argument("--model", required=True, help="OpenAI model name (e.g., gpt-4.1-nano, gpt-4.1-mini, gpt-5.4)")
    ap.add_argument("--run-id", default="1")
    ap.add_argument("--max-tool-calls", type=int, default=10, help="per-user-message tool-call cap")
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--tasks-file", default="tasks.json")
    ap.add_argument("--seed-db", default="seed_db.json")
    ap.add_argument("--policy", default="policy.md")
    args = ap.parse_args()

    tasks_doc = json.loads((REPO / args.tasks_file).read_text())
    test = find_test(tasks_doc, args.test)
    if test is None:
        raise SystemExit(f"test_id not found in {args.tasks_file}: {args.test}")

    seed = json.loads((REPO / args.seed_db).read_text())
    policy = (REPO / args.policy).read_text()
    db = copy.deepcopy(seed)

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

    out_dir = REPO / args.out_dir / _safe(args.model) / args.test / f"run_{args.run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trace.jsonl").write_text("\n".join(json.dumps(r, default=str) for r in runner.trace) + "\n")
    (out_dir / "final_db.json").write_text(json.dumps(db, indent=2))
    (out_dir / "messages.json").write_text(json.dumps(runner.assistant_messages, indent=2))
    (out_dir / "score.json").write_text(json.dumps(score, indent=2))
    (out_dir / "turn_summaries.json").write_text(json.dumps(runner.turn_summaries, indent=2))

    print(f"\nscore: {score['score']} (passed={score['passed']})")
    for r in score["results"]:
        flag = "PASS" if r["passed"] else "FAIL"
        print(f"  [{flag}] w={r['weight']}  {r['kind']:24s}  {r['rationale'][:80]}")
    print(f"\noutput: {out_dir.relative_to(REPO)}")


if __name__ == "__main__":
    main()
