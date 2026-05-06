"""
Render a shareable markdown report from the runs/ directory.

Usage:
    .venv/bin/python tools/render_report.py
    .venv/bin/python tools/render_report.py --scan-all
    .venv/bin/python tools/render_report.py --sweep runs/sweep_summary_1234.json
    .venv/bin/python tools/render_report.py --output BRIEF.md

Default behavior: find the latest `runs/sweep_summary_*.json` and render that.
With --scan-all: walk every `runs/<model>/<test>/run_*/score.json` and aggregate.
With --sweep <path>: use that specific sweep summary.

Outputs:
    REPORT.md (top-level)         — human-readable markdown report
    runs/results_summary.csv      — machine-readable table for spreadsheet pivots

The report is structured for a quick reviewer pass:
    1. Headline finding (one paragraph)
    2. Results matrix (model × task) with ASCII bars
    3. Per-test detail table (mean / min / max / runs)
    4. Failure-mode breakdown
    5. Sample trajectory excerpt (worst run for the headline test)
    6. Methodology summary

The script tolerates partial / mixed run data: missing scores skip cleanly,
older runs without `outcome.json` are handled.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"


# ---------- Data shape ----------

@dataclass
class CellRow:
    model: str
    test_id: str
    run_id: str
    score: float
    passed: bool
    failed_assertions: list[dict] = field(default_factory=list)
    outcome: str | None = None
    quote_or_order_price: float | None = None  # for negotiation tests
    adversary_model: str | None = None


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _scan_runs() -> list[CellRow]:
    rows: list[CellRow] = []
    if not RUNS.exists():
        return rows
    for model_dir in sorted(RUNS.iterdir()):
        if not model_dir.is_dir() or model_dir.name.startswith("_"):
            continue
        for test_dir in sorted(model_dir.iterdir()):
            if not test_dir.is_dir():
                continue
            for run_dir in sorted(test_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                score = _read_json(run_dir / "score.json")
                if score is None:
                    continue
                outcome = _read_json(run_dir / "outcome.json") or {}
                final_db = _read_json(run_dir / "final_db.json") or {}
                # Best-effort extraction of the final negotiated price for negotiation tests
                price = None
                if test_dir.name.startswith("4."):
                    new_orders = [
                        o for o in final_db.get("orders", [])
                        if (o.get("placed_at") or "") > "2026-04-30"  # excludes seed orders
                    ]
                    if new_orders:
                        latest = max(new_orders, key=lambda o: o.get("placed_at") or "")
                        items = latest.get("items") or []
                        if items:
                            price = float(items[0].get("unit_price_usd") or 0) or None
                rows.append(CellRow(
                    model=model_dir.name,
                    test_id=test_dir.name,
                    run_id=run_dir.name,
                    score=float(score.get("score", 0)),
                    passed=bool(score.get("passed", False)),
                    failed_assertions=[
                        {"kind": r["kind"], "weight": r["weight"], "rationale": r.get("rationale", "")[:120]}
                        for r in score.get("results", []) if not r.get("passed", False)
                    ],
                    outcome=(outcome or {}).get("outcome"),
                    quote_or_order_price=price,
                    adversary_model=(outcome or {}).get("adversary_model"),
                ))
    return rows


def _from_sweep(sweep_path: Path) -> list[CellRow]:
    sweep = _read_json(sweep_path) or {}
    rows: list[CellRow] = []
    for r in sweep.get("results", []):
        if "error" in r:
            continue
        rows.append(CellRow(
            model=r.get("model", "?"),
            test_id=r.get("test_id", "?"),
            run_id=str(r.get("run_id", "?")),
            score=float(r.get("score", 0)),
            passed=bool(r.get("passed", False)),
            failed_assertions=r.get("failed_assertions", []),
            outcome=None,
            quote_or_order_price=None,
            adversary_model=None,
        ))
    return rows


def _latest_sweep() -> Path | None:
    if not RUNS.exists():
        return None
    sweeps = sorted(RUNS.glob("sweep_summary_*.json"))
    return sweeps[-1] if sweeps else None


# ---------- Aggregation ----------

@dataclass
class Cell:
    model: str
    test_id: str
    runs: list[CellRow]

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def mean(self) -> float:
        return statistics.mean(r.score for r in self.runs) if self.runs else 0.0

    @property
    def stdev(self) -> float:
        if len(self.runs) < 2:
            return 0.0
        return statistics.stdev(r.score for r in self.runs)

    @property
    def min(self) -> float:
        return min(r.score for r in self.runs) if self.runs else 0.0

    @property
    def max(self) -> float:
        return max(r.score for r in self.runs) if self.runs else 0.0

    @property
    def passed(self) -> int:
        return sum(1 for r in self.runs if r.passed)


def _group(rows: list[CellRow]) -> dict[tuple[str, str], Cell]:
    cells: dict[tuple[str, str], Cell] = {}
    by_key: dict[tuple[str, str], list[CellRow]] = defaultdict(list)
    for r in rows:
        by_key[(r.model, r.test_id)].append(r)
    for k, lst in by_key.items():
        cells[k] = Cell(model=k[0], test_id=k[1], runs=lst)
    return cells


# ---------- Rendering helpers ----------

def _bar(value: float, width: int = 20) -> str:
    """ASCII bar of width <width> for value in [0, 1]."""
    if value < 0:
        value = 0
    if value > 1:
        value = 1
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


def _short_test(test_id: str) -> str:
    """Strip leading category for compactness in tables."""
    return test_id.split("_", 1)[-1] if "_" in test_id else test_id


def _category(test_id: str) -> str:
    head = test_id.split("_", 1)[0]
    if head.startswith("1.") or head.startswith("2.") or head.startswith("3."):
        return "policy refusal"
    if head.startswith("4."):
        return "negotiation"
    return "other"


# ---------- Report sections ----------

def _section_header(rows: list[CellRow], source: str) -> str:
    models = sorted(set(r.model for r in rows))
    tests = sorted(set(r.test_id for r in rows))
    n_runs = len(rows)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "# AurumDesk — Latest Benchmark Results\n\n"
        f"*Generated: {now}. Source: {source}.*\n\n"
        f"**{n_runs} run(s)** across **{len(models)} model(s)** and **{len(tests)} test(s)**.\n"
    )


def _section_headline(cells: dict[tuple[str, str], Cell], rows: list[CellRow]) -> str:
    """Compute and write the headline finding."""
    # By category × model
    by_cat_model: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        by_cat_model[(_category(r.test_id), r.model)].append(r.score)

    models = sorted(set(r.model for r in rows))

    # Negotiation cell summary
    neg_means: dict[str, float] = {}
    for m in models:
        scores = by_cat_model.get(("negotiation", m), [])
        if scores:
            neg_means[m] = statistics.mean(scores)

    refusal_means: dict[str, float] = {}
    for m in models:
        scores = by_cat_model.get(("policy refusal", m), [])
        if scores:
            refusal_means[m] = statistics.mean(scores)

    # Below-floor breach count
    breaches = 0
    for r in rows:
        for fa in r.failed_assertions:
            rationale = (fa.get("rationale") or "").lower()
            if "below_floor" in rationale or "never breach" in rationale or "no below-floor" in rationale.lower():
                breaches += 1
                break

    out = "## Headline\n\n"

    if neg_means and refusal_means:
        sorted_models = sorted(neg_means.keys(), key=lambda m: refusal_means.get(m, 0), reverse=True)
        strongest = sorted_models[0]
        out += (
            f"This benchmark stress-tests an AI customer-service agent's ability to follow policy under adversarial "
            f"pressure (Phase 1) AND to negotiate competently when leverage favors them (Phase 2). The two phases "
            f"surface different failure modes:\n\n"
        )
        out += f"- **Phase 1 (policy refusal):** {strongest} averages **{refusal_means[strongest]:.2f}**. "
        weakest_models = [m for m in sorted_models if refusal_means.get(m, 1.0) < 0.5]
        if weakest_models:
            out += f"Weaker models ({', '.join(weakest_models)}) fail consistently.\n"
        else:
            out += "All tested models pass most refusal scenarios.\n"
        out += (
            f"- **Phase 2 (negotiation):** {strongest} captures only **{neg_means[strongest]*100:.0f}%** "
            f"of the available scoring band on negotiation tasks. "
            f"This is the headline finding — frontier models do *not* meaningfully negotiate above floor "
            f"by default, even when context signals say leverage favors them.\n"
        )
        if breaches:
            out += (
                f"- **{breaches}** of {len(rows)} runs ({100*breaches/len(rows):.0f}%) involved at least one "
                f"hard policy breach (typically a below-floor binding quote under buyer pressure).\n"
            )
    else:
        out += (
            "Insufficient data across categories to produce a headline finding — "
            "run more cells via `agent/run_sweep.py` and re-render.\n"
        )
    return out + "\n"


def _section_matrix(cells: dict[tuple[str, str], Cell], rows: list[CellRow]) -> str:
    out = "## Results matrix\n\n"
    out += "Mean score per (model, test). Bar = score on the [0, 1] scale; (n) = number of runs.\n\n"

    models = sorted(set(r.model for r in rows))
    tests = sorted(set(r.test_id for r in rows))

    # Compact format: one row per (model, test) pair, sorted
    out += "| Model | Test | Mean | Bar | Runs |\n"
    out += "|---|---|---:|---|---:|\n"
    for m in models:
        for t in tests:
            cell = cells.get((m, t))
            if not cell:
                continue
            out += f"| {m} | `{t}` | {cell.mean:.2f} | `{_bar(cell.mean)}` | {cell.n} |\n"

    # Per-model overall
    out += "\n### Per-model overall\n\n"
    out += "| Model | Overall mean | Bar | Runs |\n"
    out += "|---|---:|---|---:|\n"
    for m in models:
        scores = [r.score for r in rows if r.model == m]
        if not scores:
            continue
        mean = statistics.mean(scores)
        out += f"| {m} | {mean:.2f} | `{_bar(mean)}` | {len(scores)} |\n"
    return out + "\n"


def _section_per_test(cells: dict[tuple[str, str], Cell], rows: list[CellRow]) -> str:
    out = "## Per-test detail\n\n"
    out += "| Test | Type | Models | Runs | Mean | Min | Max | Stdev |\n"
    out += "|---|---|---|---:|---:|---:|---:|---:|\n"
    tests = sorted(set(r.test_id for r in rows))
    for t in tests:
        rs = [r for r in rows if r.test_id == t]
        if not rs:
            continue
        models = sorted(set(r.model for r in rs))
        scores = [r.score for r in rs]
        mn = statistics.mean(scores)
        sd = statistics.stdev(scores) if len(scores) > 1 else 0.0
        out += (
            f"| `{t}` | {_category(t)} | {len(models)} "
            f"| {len(rs)} | {mn:.2f} | {min(scores):.2f} | {max(scores):.2f} | {sd:.2f} |\n"
        )
    return out + "\n"


def _section_failure_modes(rows: list[CellRow]) -> str:
    out = "## Failure modes\n\n"
    out += "Most-frequent failed assertion across all runs. (A run can contribute multiple failures.)\n\n"
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        for fa in r.failed_assertions:
            kind = fa.get("kind", "?")
            rationale = (fa.get("rationale") or "")[:100]
            counts[(kind, rationale)] += 1
    if not counts:
        return out + "*(none recorded)*\n\n"
    sorted_fails = sorted(counts.items(), key=lambda kv: -kv[1])
    out += "| Count | Kind | Rationale |\n"
    out += "|---:|---|---|\n"
    for (kind, rationale), c in sorted_fails[:12]:
        out += f"| {c} | `{kind}` | {rationale} |\n"
    return out + "\n"


def _section_sample_trajectory(rows: list[CellRow]) -> str:
    out = "## Sample trajectory (lowest-scoring run)\n\n"
    if not rows:
        return out + "*(no data)*\n\n"

    # Prefer a negotiation run if any are present (negotiation is the headline);
    # fall back to overall-worst otherwise.
    neg_runs = [r for r in rows if _category(r.test_id) == "negotiation"]
    candidates = neg_runs if neg_runs else rows
    worst = min(candidates, key=lambda r: r.score)

    # run_id from the sweep is bare ("3") but on-disk dirs are "run_3";
    # fall back to the prefixed form when needed.
    candidates_path = [
        RUNS / worst.model / worst.test_id / worst.run_id,
        RUNS / worst.model / worst.test_id / f"run_{worst.run_id}",
    ]
    run_path = next((p for p in candidates_path if (p / "messages.json").exists()), None)
    if run_path is None:
        return out + f"*(worst run was {worst.model} / {worst.test_id} / run {worst.run_id} but no message data was saved)*\n\n"
    msgs_path = run_path / "messages.json"
    adv_msgs_path = run_path / "adversary_messages.json"

    agent_msgs = _read_json(msgs_path) or []
    adv_msgs = _read_json(adv_msgs_path) or []

    # Fallback for scripted tests (no adversary_messages.json): load the fixed
    # user_messages from tasks.json so the buyer side of the trajectory is visible.
    if not adv_msgs:
        tasks_doc = _read_json(REPO / "tasks.json") or {}
        for task in tasks_doc.get("tasks", []):
            for test in task.get("tests", []):
                if test.get("test_id") == worst.test_id and "user_messages" in test:
                    adv_msgs = test["user_messages"]
                    break

    out += (
        f"**Run:** `{worst.model}` / `{worst.test_id}` / `{worst.run_id}` "
        f"(score: **{worst.score:.2f}**, outcome: `{worst.outcome or 'n/a'}`)\n\n"
    )
    n = min(3, max(len(adv_msgs), len(agent_msgs)))
    for i in range(n):
        if i < len(adv_msgs):
            text = adv_msgs[i].strip()
            if len(text) > 400:
                text = text[:400] + "…"
            out += f"**Buyer (turn {i+1}):**\n\n> {text}\n\n"
        if i < len(agent_msgs):
            text = agent_msgs[i].strip()
            if len(text) > 400:
                text = text[:400] + "…"
            out += f"**Seller (turn {i+1}):**\n\n> {text}\n\n"
    if worst.failed_assertions:
        out += "**Failed assertions:**\n\n"
        for fa in worst.failed_assertions:
            out += f"- `{fa.get('kind')}` (weight {fa.get('weight')}): {fa.get('rationale', '')[:120]}\n"
        out += "\n"
    return out


def _section_methodology() -> str:
    return (
        "## Methodology — quick read\n\n"
        "- **Two test modes.** *Scripted* (Phase 1): a fixed list of user messages is sent regardless of agent reply. "
        "*Two-agent* (Phase 2): an adversary LLM plays the buyer with its own goals and tools (incl. `accept_quote` to close deals on its own side, so the seller can't unilaterally finalize).\n"
        "- **Outcome-based grading.** Six assertion kinds, including `negotiation_zopa_score` for partial-credit price scoring and `judge_message_satisfies` for LLM-as-judge over free-form criteria. Score = Σ(weight × partial) / Σ(weight).\n"
        "- **Synthetic, no real PII.** All companies, customers, prices, and addresses are invented for the benchmark.\n"
        "- **Reproducible.** All artifacts (transcripts, DB state, trace, judge cache) are saved per run under `runs/`.\n\n"
        "Full details: see [`README.md`](README.md) and [`PHASE_2_PLAN.md`](PHASE_2_PLAN.md).\n"
    )


# ---------- CSV ----------

def _write_csv(rows: list[CellRow], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "test_id", "run_id", "score", "passed", "outcome", "category", "negotiation_price"])
        for r in rows:
            w.writerow([
                r.model, r.test_id, r.run_id, f"{r.score:.4f}", int(r.passed),
                r.outcome or "", _category(r.test_id), r.quote_or_order_price or "",
            ])


# ---------- Main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", help="path to a specific sweep_summary_*.json")
    ap.add_argument("--scan-all", action="store_true", help="walk every runs/<model>/<test>/run_*/score.json instead of using a sweep summary")
    ap.add_argument("--output", default="REPORT.md", help="markdown output path (default: REPORT.md)")
    ap.add_argument("--csv", default="runs/results_summary.csv", help="csv output path")
    args = ap.parse_args()

    if args.sweep:
        rows = _from_sweep(Path(args.sweep))
        source = f"sweep file `{Path(args.sweep).name}`"
    elif args.scan_all:
        rows = _scan_runs()
        source = "scanned `runs/` directory"
    else:
        latest = _latest_sweep()
        if latest is not None:
            rows = _from_sweep(latest)
            source = f"latest sweep `{latest.name}`"
        else:
            rows = _scan_runs()
            source = "scanned `runs/` directory (no sweep summary found)"

    if not rows:
        out_path = Path(args.output)
        out_path.write_text(
            "# AurumDesk Benchmark — No Results Yet\n\n"
            "No runs found under `runs/`. Run a sweep first:\n\n"
            "```bash\n"
            ".venv/bin/python -m agent.run_sweep --models gpt-5.4 --runs 1\n"
            "```\n"
        )
        print(f"No runs found. Wrote stub to {out_path}.")
        return

    cells = _group(rows)

    parts = [
        _section_header(rows, source),
        _section_headline(cells, rows),
        _section_matrix(cells, rows),
        _section_per_test(cells, rows),
        _section_failure_modes(rows),
        _section_sample_trajectory(rows),
        _section_methodology(),
    ]
    md = "\n".join(parts)

    out_path = REPO / args.output if not Path(args.output).is_absolute() else Path(args.output)
    out_path.write_text(md)

    csv_path = REPO / args.csv if not Path(args.csv).is_absolute() else Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(rows, csv_path)

    print(f"✓ wrote {out_path.relative_to(REPO)} ({len(md):,} chars, {len(rows)} runs)")
    print(f"✓ wrote {csv_path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
