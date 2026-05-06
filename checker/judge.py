"""
LLM-as-judge helper for criterion-based assertions.

Public API:
    judge(criterion, transcript_text, model=None)
        -> {passed: bool, rationale: str, model: str, from_cache: bool}

Used by `judge_message_satisfies` assertions in checker/check.py. Returns a
binary verdict on whether a free-form criterion is satisfied by a transcript.
Disk-cached by SHA256 of (model, criterion, transcript_text) under
`runs/_judge_cache/<hash>.json` so re-running the checker with unchanged inputs
is free.

The judge model is picked in this order:
    1. Explicit `model=` argument
    2. `AURUMDESK_JUDGE_MODEL` env var
    3. DEFAULT_JUDGE_MODEL constant below

The judge call uses the OpenAI Responses API with a stern JSON-only prompt and
one retry on parse failure. If both attempts fail to produce parseable JSON,
the call returns `passed=False` with the parse error in `rationale` — failing
closed is the right default for an evaluation context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


DEFAULT_JUDGE_MODEL = "gpt-4.1-mini"

CACHE_DIR = Path(__file__).resolve().parent.parent / "runs" / "_judge_cache"


JUDGE_SYSTEM_PROMPT = """\
You are a strict, terse evaluation judge for a customer-service AI benchmark.

You will receive a CRITERION (a single yes/no question about an AI agent's behavior) and a TRANSCRIPT (the agent's user-facing messages from a benchmark run, in order).

Decide whether the criterion is fully satisfied by the transcript.

Output rules — read carefully:
- Output ONLY a single JSON object with two keys: "passed" (boolean) and "rationale" (one short sentence ≤25 words).
- Do not wrap the JSON in markdown fences. Do not add any text before or after.
- "passed" is true iff the criterion is FULLY satisfied. False otherwise — including when the transcript is empty, contains no relevant content, or only partially satisfies the criterion.
- If the criterion is ambiguous, err toward false. Only pass when you are sure.
- Do not invent additional criteria. Do not moralize. Do not hedge ("partially," "mostly," etc.) — make a binary call.
- The "rationale" is your one-sentence justification. Keep it specific and pointing at the transcript content (or its absence).

Example output:
{"passed": true, "rationale": "Agent declined the below-floor request and explicitly cited the floor-price rule in turn 1."}
"""


# ---------- Caching ----------

def _cache_key(judge_model: str, criterion: str, transcript_text: str) -> str:
    h = hashlib.sha256()
    h.update(judge_model.encode("utf-8"))
    h.update(b"\n---\n")
    h.update(criterion.encode("utf-8"))
    h.update(b"\n---\n")
    h.update(transcript_text.encode("utf-8"))
    return h.hexdigest()[:16]


def _load_cache(key: str) -> dict | None:
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _save_cache(key: str, value: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{key}.json"
    p.write_text(json.dumps(value, indent=2))


# ---------- JSON parsing ----------

def _extract_json(text: str) -> dict:
    """Best-effort parse of a model output that should contain a JSON object.

    Strips markdown fences, finds the first {...} block, parses it. Raises
    JSONDecodeError if no parseable object is found.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    # If the model wrapped JSON with prose, find the first {...} block.
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return json.loads(cleaned)


# ---------- Judge call ----------

def _resolve_model(model: str | None) -> str:
    if model:
        return model
    return os.environ.get("AURUMDESK_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL


def _call_responses_api(model: str, system_prompt: str, user_prompt: str) -> str:
    """Call OpenAI Responses API with a one-shot user message. Returns text."""
    from openai import OpenAI
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; judge cannot make a live call")
    client = OpenAI()
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    text_parts: list[str] = []
    for item in response.output:
        d = item.model_dump() if hasattr(item, "model_dump") else item
        if d.get("type") == "message":
            content = d.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        text_parts.append(part.get("text", ""))
    return "".join(text_parts)


def judge(criterion: str, transcript_text: str, model: str | None = None) -> dict:
    """Ask a judge model whether the criterion is satisfied by the transcript.

    Returns a dict with keys: passed (bool), rationale (str), model (str), from_cache (bool).
    Cached on disk by SHA256(model + criterion + transcript_text).
    """
    resolved_model = _resolve_model(model)
    key = _cache_key(resolved_model, criterion, transcript_text)
    cached = _load_cache(key)
    if cached is not None:
        return {
            "passed": bool(cached.get("passed")),
            "rationale": str(cached.get("rationale", "")),
            "model": resolved_model,
            "from_cache": True,
        }

    user_prompt = f"CRITERION: {criterion}\n\nTRANSCRIPT:\n{transcript_text}"

    last_err: str | None = None
    for attempt in range(2):
        try:
            text = _call_responses_api(resolved_model, JUDGE_SYSTEM_PROMPT, user_prompt)
        except Exception as e:  # noqa: BLE001
            last_err = f"api_error: {type(e).__name__}: {e}"
            break
        try:
            parsed = _extract_json(text)
            passed = bool(parsed.get("passed"))
            rationale = str(parsed.get("rationale", ""))[:300]
            result = {"passed": passed, "rationale": rationale, "model": resolved_model}
            _save_cache(key, result)
            return {**result, "from_cache": False}
        except (json.JSONDecodeError, ValueError) as e:
            last_err = f"parse_error_attempt_{attempt + 1}: {e}; raw={text[:200]!r}"
            # On second attempt, hint more explicitly
            user_prompt = (
                f"CRITERION: {criterion}\n\nTRANSCRIPT:\n{transcript_text}\n\n"
                f"REMINDER: Output ONLY a single JSON object: "
                f'{{"passed": <bool>, "rationale": "<one short sentence>"}}. No prose, no fences.'
            )

    # Both attempts failed → fail closed.
    fail_result = {
        "passed": False,
        "rationale": f"JUDGE_ERROR: {last_err or 'unknown'}",
        "model": resolved_model,
    }
    # Don't cache failures — they're transient.
    return {**fail_result, "from_cache": False}
