"""
Run a matrix sweep: every (model × test) cell × N runs, in parallel.

Usage:
    python -m agent.run_sweep --models gpt-5.4 gpt-4.1-mini gpt-4.1-nano --runs 3
    python -m agent.run_sweep --models gpt-5.4 --tests 2.1_jailbreak_plus_investment_dd --runs 5
    python -m agent.run_sweep --models gpt-4.1-nano --runs 1 --parallel 1     # smoke

Defaults:
    --tests   = every test in tasks.json
    --runs    = 1
    --parallel = 4

Writes per-cell artifacts under runs/<model>/<test>/run_<n>/ (same layout as
run_agent.py). Writes a sweep-level summary file at runs/sweep_summary_<ts>.json.
Prints a results matrix at the end (rows=models, cols=tests, cells=mean score).
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from agent import tools as agent_tools
from agent.models import AgentRunner
from agent.providers import OpenAIProvider, Provider
from checker.check import evaluate, find_test


REPO = Path(__file__).resolve().parent.parent


def _safe(name: str) -> str:
    return name.replace("/", "_").replace(":", "_")


# ---------- Provider factory (modular: add Anthropic etc. here) ----------

def make_provider(model: str, system_prompt: str, tools: list[dict], max_tool_calls: int) -> Provider:
    # Heuristic: anything starting with "gpt", "o" (o-series), or "openai/" → OpenAI.
    # Future: dispatch to AnthropicProvider for "claude-...", etc.
    return OpenAIProvider(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        max_tool_calls_per_turn=max_tool_calls,
    )


# ---------- Per-cell runner ----------

def run_cell(
    model: str,
    test_id: str,
    run_id: int,
    tasks_doc: dict,
    seed: dict,
    policy: str,
    out_root: Path,
    max_tool_calls: int,
) -> dict:
    """Run one (model, test, run_id) cell. Always returns a dict; never raises."""
    test = find_test(tasks_doc, test_id)
    if test is None:
        return {
            "model": model, "test_id": test_id, "run_id": run_id,
            "error": f"unknown test_id: {test_id}",
        }

    db = copy.deepcopy(seed)

    try:
        provider = make_provider(model, policy, agent_tools.TOOL_SCHEMAS, max_tool_calls)
        runner = AgentRunner(provider, db)

        start = time.time()
        runner.run_user_messages(test["user_messages"])
        elapsed = time.time() - start

        score = evaluate(test["assertions"], db, runner.assistant_messages)

        out_dir = out_root / _safe(model) / test_id / f"run_{run_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "trace.jsonl").write_text(
            "\n".join(json.dumps(r, default=str) for r in runner.trace) + "\n"
        )
        (out_dir / "final_db.json").write_text(json.dumps(db, indent=2))
        (out_dir / "messages.json").write_text(json.dumps(runner.assistant_messages, indent=2))
        (out_dir / "score.json").write_text(json.dumps(score, indent=2))
        (out_dir / "turn_summaries.json").write_text(json.dumps(runner.turn_summaries, indent=2))

        return {
            "model": model,
            "test_id": test_id,
            "run_id": run_id,
            "score": score["score"],
            "passed": score["passed"],
            "earned_weight": score["earned_weight"],
            "total_weight": score["total_weight"],
            "elapsed_s": round(elapsed, 1),
            "tool_calls_total": sum(s["tool_calls_used"] for s in runner.turn_summaries),
            "turn_statuses": [s["status"] for s in runner.turn_summaries],
            "n_assistant_messages": len(runner.assistant_messages),
            "out_dir": str(out_dir.relative_to(REPO)),
            "failed_assertions": [
                {"kind": r["kind"], "weight": r["weight"], "rationale": r["rationale"]}
                for r in score["results"] if not r["passed"]
            ],
        }
    except Exception as e:
        return {
            "model": model,
            "test_id": test_id,
            "run_id": run_id,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }


# ---------- Output formatting ----------

def print_progress(done: int, total: int, res: dict) -> None:
    if "error" in res:
        print(f"  [{done}/{total}] ERROR  {res.get('model','?'):18s} {res.get('test_id','?'):<40s} run {res.get('run_id','?')}: {res['error']}")
        return
    flag = "PASS" if res["passed"] else "FAIL"
    print(
        f"  [{done}/{total}] {flag}   {res['model']:18s} {res['test_id']:<40s} run {res['run_id']}  "
        f"score={res['score']:.2f}  {res['elapsed_s']:>5.1f}s  tools={res['tool_calls_total']}"
    )


def print_matrix(results: list[dict], models: list[str], tests: list[str]) -> None:
    """2-D table: rows=models, cols=tests, cells='<mean> (<passed>/<n>)'."""
    cells: dict[tuple[str, str], list[dict]] = {}
    for r in results:
        if "error" in r:
            continue
        cells.setdefault((r["model"], r["test_id"]), []).append(r)

    test_w = max(22, max((len(t) for t in tests), default=22)) + 2
    model_w = max(15, max((len(m) for m in models), default=15)) + 2

    bar = "─" * (model_w + test_w * len(tests) + 12)
    print("\n" + bar)
    print("RESULTS MATRIX  —  cells show: mean_score (passed/n)")
    print(bar)

    header = f"{'model':<{model_w}}"
    for t in tests:
        header += f"{t:<{test_w}}"
    header += f"{'overall':>10}"
    print(header)
    print("─" * len(header))

    for model in models:
        row = f"{model:<{model_w}}"
        all_scores: list[float] = []
        for t in tests:
            cell = cells.get((model, t), [])
            if not cell:
                row += f"{'-':<{test_w}}"
                continue
            scores = [r["score"] for r in cell]
            passed = sum(1 for r in cell if r["passed"])
            mean = sum(scores) / len(scores)
            all_scores.extend(scores)
            row += f"{mean:.2f} ({passed}/{len(scores)})".ljust(test_w)
        if all_scores:
            row += f"{sum(all_scores) / len(all_scores):>10.2f}"
        else:
            row += f"{'-':>10}"
        print(row)
    print(bar)

    # Per-test per-model failure-mode breakdown
    print("\nFailure-mode breakdown (first failed assertion per failing run):")
    for model in models:
        for t in tests:
            cell = cells.get((model, t), [])
            failures = [r for r in cell if not r["passed"]]
            if not failures:
                continue
            for r in failures:
                fa = r.get("failed_assertions", [])
                if fa:
                    print(f"  {model} / {t} / run {r['run_id']}: {fa[0]['kind']} (w={fa[0]['weight']}) — {fa[0]['rationale'][:90]}")
                else:
                    print(f"  {model} / {t} / run {r['run_id']}: (no failed assertions reported)")

    errors = [r for r in results if "error" in r]
    if errors:
        print(f"\n{len(errors)} cells errored:")
        for r in errors:
            print(f"  {r.get('model','?')} {r.get('test_id','?')} run {r.get('run_id','?')}: {r['error']}")


# ---------- CLI ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--tests", nargs="*", help="default: every test in tasks.json")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--max-tool-calls", type=int, default=10)
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--tasks-file", default="tasks.json")
    ap.add_argument("--seed-db", default="seed_db.json")
    ap.add_argument("--policy", default="policy.md")
    args = ap.parse_args()

    tasks_doc = json.loads((REPO / args.tasks_file).read_text())
    seed = json.loads((REPO / args.seed_db).read_text())
    policy = (REPO / args.policy).read_text()

    if args.tests:
        tests = list(args.tests)
    else:
        tests = [t["test_id"] for task in tasks_doc["tasks"] for t in task["tests"]]

    out_root = REPO / args.out_dir
    out_root.mkdir(parents=True, exist_ok=True)

    cells = list(itertools.product(args.models, tests, range(1, args.runs + 1)))
    print(
        f"Running {len(cells)} cells: {len(args.models)} model(s) × {len(tests)} test(s) × {args.runs} run(s) "
        f"(parallel={args.parallel})\n"
    )

    sweep_start = time.time()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = [
            ex.submit(run_cell, m, t, r, tasks_doc, seed, policy, out_root, args.max_tool_calls)
            for m, t, r in cells
        ]
        total = len(futures)
        for done, fut in enumerate(as_completed(futures), start=1):
            res = fut.result()
            results.append(res)
            print_progress(done, total, res)

    elapsed = time.time() - sweep_start

    summary_path = out_root / f"sweep_summary_{int(time.time())}.json"
    summary_path.write_text(json.dumps({
        "config": vars(args),
        "elapsed_s": round(elapsed, 1),
        "results": results,
    }, indent=2))

    print_matrix(results, args.models, tests)
    print(f"\nelapsed: {elapsed:.1f}s   summary: {summary_path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
