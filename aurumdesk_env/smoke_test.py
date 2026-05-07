"""One-rollout smoke test for the verifiers wrapper.

Picks the cheapest sensible combination (gpt-4.1-nano on both sides) and runs
a single rollout end-to-end. Exits non-zero on any failure. Output goes to
stdout so it can be inspected directly.

Usage:
    python -m aurumdesk_env.smoke_test
    python -m aurumdesk_env.smoke_test --test 4.2_palladium_no_buyer_anchor --model gpt-4.1-nano
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from openai import AsyncOpenAI

from aurumdesk_env import load_environment


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", default="1.2_adaptive_lowball")
    ap.add_argument("--model", default="gpt-4.1-nano")
    ap.add_argument("--adversary-model", default="gpt-4.1-nano")
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print(f"Loading env (test={args.test}, adversary={args.adversary_model})")
    env = load_environment(
        test_ids=[args.test],
        adversary_model=args.adversary_model,
    )
    row = env.dataset[0]
    print(f"  test_id: {row['info']['test_id']}")
    print(f"  max_rounds: {row['info']['max_rounds']}")
    print(f"  scenario_cue: {row['info']['scenario_cue'][:80]}...")
    print()

    client = AsyncOpenAI()
    print(f"Running rollout: seller={args.model}, adversary={args.adversary_model}")
    start = time.time()
    completion, state = asyncio.run(
        env.rollout(
            client=client,
            model=args.model,
            prompt=row["prompt"],
            answer=row["answer"],
            task=row["task"],
            info=row["info"],
        )
    )
    elapsed = time.time() - start

    # Outcome summary
    outcome = (
        "accepted" if state.get("accepted")
        else "gave_up" if state.get("gave_up")
        else "adversary_silent" if state.get("adversary_silent")
        else "seller_silent" if state.get("seller_silent")
        else "max_rounds_hit" if state.get("max_rounds_hit")
        else "unknown"
    )
    print(f"\n--- ROLLOUT DONE ({elapsed:.1f}s) ---")
    print(f"  outcome: {outcome}")
    print(f"  seller_text_count: {state.get('seller_text_count')}")
    print(f"  completion length: {len(completion)} messages")
    print(f"  adversary_messages: {len(state.get('adversary_messages', []))} turns")
    print()

    # Score via env's reward func
    print("Scoring via rubric...")
    scores = asyncio.run(
        env.rubric.score_rollout(
            prompt=row["prompt"],
            completion=completion,
            answer=row["answer"],
            state=state,
            task=row["task"],
            info=row["info"],
        )
    )
    print(f"  reward: {scores.reward}")
    print(f"  metrics: {scores.metrics}")
    print()

    breakdown = state.get("aurumdesk_breakdown")
    if breakdown:
        print("Assertion breakdown:")
        for r in breakdown.get("results", []):
            flag = "PASS" if r["passed"] else "FAIL"
            print(f"  [{flag}] w={r['weight']} {r['kind']:24s} {r['rationale'][:80]}")
        print(f"  Total: {breakdown['score']:.3f} (passed_weight={breakdown['earned_weight']}, total_weight={breakdown['total_weight']})")
    print()

    # Sample seller / adversary messages
    print("Conversation tail:")
    seller_msgs = [m for m in completion if m.get("role") == "assistant" and m.get("content")]
    adv_msgs = state.get("adversary_messages", [])
    print(f"  seller's last message: {seller_msgs[-1]['content'][:200]!r}..." if seller_msgs else "  (no seller messages)")
    print(f"  adversary's last message: {adv_msgs[-1][:200]!r}..." if adv_msgs else "  (no adversary messages)")


if __name__ == "__main__":
    main()
