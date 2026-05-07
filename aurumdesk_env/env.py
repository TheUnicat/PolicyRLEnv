"""AurumDesk two-agent negotiation environment, wrapped as a verifiers.MultiTurnEnv.

Architecture:
- Each row in the dataset = one test from `tasks.json` (negotiation tests only).
- The seller-side agent (the model under test) runs through verifiers' Chat
  Completions path. Its tools (from `agent.tools.TOOL_SCHEMAS`) are converted
  to Chat-Completions format and passed via the env's `oai_tools` so the model
  receives them on every call.
- The adversary (buyer-side) runs through our existing `OpenAIProvider`
  (Responses API, sync) inside `env_response`. The adversary's tools are
  `give_up` and `accept_quote`; the buyer-side dispatcher (closing over `db`)
  handles them.
- The adversary's opening turn happens inside the overridden `rollout()`,
  *before* the verifiers loop starts, so the seller's first user message is
  the realistic opening line (not the bracketed director cue).

Termination matches the existing `TwoAgentRunner` semantics:
- max_rounds reached (counted as completed seller-text turns)
- buyer calls `accept_quote` -> `state['accepted']`
- buyer or seller goes silent
- buyer calls `give_up` -> `state['gave_up']`
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import verifiers as vf
from datasets import Dataset

from agent import tools as agent_tools
from agent.adversary_tools import (
    ADVERSARY_TOOL_SCHEMAS,
    make_adversary_dispatcher,
)
from agent.providers import OpenAIProvider
from checker.check import evaluate


REPO = Path(__file__).resolve().parent.parent
DEFAULT_ADVERSARY_MODEL = "gpt-5.4-mini"


# ---------- Tool-schema conversion (Responses-API flat -> Chat-Completions) ----------

def _to_chat_tool(schema: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema["parameters"],
        },
    }


SELLER_OAI_TOOLS = [_to_chat_tool(s) for s in agent_tools.TOOL_SCHEMAS]


# ---------- Dataset ----------

def _build_dataset(
    tasks_doc: dict, policy: str, test_ids: list[str] | None
) -> Dataset:
    """One row per two-agent negotiation test. Scripted tests are skipped.

    Each row's prompt is `[system: policy, user: <placeholder>]`. The placeholder
    is replaced inside `rollout()` with the adversary's opening line so the seller
    sees a realistic first message instead of the bracketed director cue.
    """
    rows = []
    for task in tasks_doc["tasks"]:
        for test in task["tests"]:
            is_two_agent = (
                "adversary_prompt_file" in test or test.get("mode") == "two_agent"
            )
            if not is_two_agent:
                continue
            if test_ids and test["test_id"] not in test_ids:
                continue
            rows.append({
                "prompt": [
                    {"role": "system", "content": policy},
                    {"role": "user", "content": "[adversary opens at rollout time]"},
                ],
                "answer": "",
                "task": "negotiation",
                "info": {
                    "test_id": test["test_id"],
                    "scenario_cue": test.get(
                        "scenario_cue",
                        "[Begin the conversation. You speak first.]",
                    ),
                    "max_rounds": int(test.get("max_rounds", 6)),
                    "adversary_prompt_file": test["adversary_prompt_file"],
                    "adversary_prompt_prepend": test.get(
                        "adversary_prompt_prepend", ""
                    ),
                    # Assertion shapes vary across kinds; serialize for arrow safety.
                    "assertions_json": json.dumps(test["assertions"]),
                },
            })
    if not rows:
        raise ValueError(
            "No two-agent negotiation tests found "
            f"(test_ids filter: {test_ids})"
        )
    return Dataset.from_list(rows)


# ---------- Environment subclass ----------

class AurumDeskNegotiationEnv(vf.MultiTurnEnv):
    def __init__(
        self,
        tasks_doc: dict,
        seed_db: dict,
        policy: str,
        adversary_model: str = DEFAULT_ADVERSARY_MODEL,
        test_ids: list[str] | None = None,
        max_seller_tool_calls: int = 10,
        **kwargs,
    ):
        dataset = _build_dataset(tasks_doc, policy, test_ids)
        super().__init__(
            dataset=dataset,
            rubric=_build_rubric(),
            oai_tools=SELLER_OAI_TOOLS,
            max_turns=200,  # high cap; our own counter (seller_text_count) drives termination
            **kwargs,
        )
        self._seed_db = seed_db
        self._adversary_model = adversary_model
        self._max_seller_tool_calls = max_seller_tool_calls

    # ----- pre-rollout: build per-rollout state, run adversary's opening turn -----

    async def rollout(
        self,
        client,
        model,
        prompt,
        answer="",
        task="default",
        info=None,
        sampling_args=None,
        **kwargs,
    ):
        info = info or {}
        if isinstance(info, str):
            info = json.loads(info)

        db = copy.deepcopy(self._seed_db)
        adv_provider = self._build_adversary_provider(info)
        adv_dispatcher = make_adversary_dispatcher(db=db)

        opening_tr = adv_provider.send_user_message(
            info["scenario_cue"], adv_dispatcher
        )
        opening_text = " ".join(
            m for m in opening_tr.assistant_messages if m
        ).strip() or "[adversary produced no opening text]"

        # Replace the placeholder user msg with the realistic opening line.
        new_prompt = [m for m in prompt if m.get("role") != "user"]
        new_prompt.append({"role": "user", "content": opening_text})

        # Stash per-rollout objects; setup_state copies them onto state.
        kwargs["_adk_db"] = db
        kwargs["_adk_adv_provider"] = adv_provider
        kwargs["_adk_adv_dispatcher"] = adv_dispatcher
        kwargs["_adk_initial_adv_text"] = opening_text

        return await super().rollout(
            client, model, new_prompt, answer, task, info, sampling_args, **kwargs,
        )

    def _build_adversary_provider(self, info: dict) -> OpenAIProvider:
        adv_path = REPO / info["adversary_prompt_file"]
        adv_prompt = adv_path.read_text()
        prepend_rel = info.get("adversary_prompt_prepend") or ""
        if prepend_rel:
            adv_prompt = (REPO / prepend_rel).read_text() + adv_prompt
        return OpenAIProvider(
            model=self._adversary_model,
            system_prompt=adv_prompt,
            tools=ADVERSARY_TOOL_SCHEMAS,
            max_tool_calls_per_turn=3,
        )

    # ----- per-rollout state initialization -----

    async def setup_state(self, state, **kwargs):
        state["db"] = kwargs["_adk_db"]
        state["adversary"] = kwargs["_adk_adv_provider"]
        state["adv_dispatcher"] = kwargs["_adk_adv_dispatcher"]
        state["adversary_messages"] = [kwargs["_adk_initial_adv_text"]]
        state["accepted"] = False
        state["gave_up"] = False
        state["adversary_silent"] = False
        state["seller_silent"] = False
        state["max_rounds_hit"] = False
        state["seller_text_count"] = 0
        return state

    # ----- termination -----

    async def is_completed(self, messages, state, **kwargs):
        base = await super().is_completed(messages, state, **kwargs)
        return bool(
            base
            or state.get("accepted")
            or state.get("gave_up")
            or state.get("adversary_silent")
            or state.get("seller_silent")
            or state.get("max_rounds_hit")
        )

    # ----- env response: dispatch seller tools or run adversary turn -----

    async def env_response(self, messages, state, **kwargs):
        if not messages:
            return [], state
        last = messages[-1]
        if last.get("role") != "assistant":
            return [], state

        tool_calls = last.get("tool_calls")
        if tool_calls:
            return await self._dispatch_seller_tools(tool_calls, state)

        state["seller_text_count"] += 1
        info = state.get("info") or {}
        max_rounds = int(info.get("max_rounds", 6))
        if state["seller_text_count"] >= max_rounds:
            state["max_rounds_hit"] = True
            return [], state

        seller_text = last.get("content") or ""
        if not seller_text:
            state["seller_silent"] = True
            return [], state

        return await self._run_adversary_turn(seller_text, state)

    async def _dispatch_seller_tools(self, tool_calls, state):
        msgs = []
        for tc in tool_calls:
            if hasattr(tc, "function"):
                name = tc.function.name
                args_str = tc.function.arguments
                tc_id = tc.id or ""
            else:
                name = tc["function"]["name"]
                args_str = tc["function"]["arguments"]
                tc_id = tc["id"]
            try:
                args = json.loads(args_str) if args_str else {}
                result = agent_tools.dispatch(state["db"], name, args)
                content = json.dumps(result)
            except Exception as e:
                content = json.dumps({"error": f"{type(e).__name__}: {e}"})
            msgs.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": content,
            })
        return msgs, state

    async def _run_adversary_turn(self, seller_text, state):
        tr = state["adversary"].send_user_message(
            seller_text, state["adv_dispatcher"]
        )
        adv_text = " ".join(m for m in tr.assistant_messages if m).strip()
        state["adversary_messages"].append(adv_text)

        for tc in tr.tool_call_log:
            if tc["name"] == "accept_quote":
                state["accepted"] = True
            elif tc["name"] == "give_up":
                state["gave_up"] = True

        if state["accepted"] or state["gave_up"]:
            return [], state
        if not adv_text:
            state["adversary_silent"] = True
            return [], state

        return [{"role": "user", "content": adv_text}], state


# ---------- Rubric ----------

def _build_rubric() -> vf.Rubric:
    async def aurumdesk_score(completion, info, state) -> float:
        if isinstance(info, str):
            info = json.loads(info)
        assertions = json.loads(info["assertions_json"])
        assistant_texts = [
            m["content"] for m in completion
            if m.get("role") == "assistant" and m.get("content")
        ]
        breakdown = evaluate(assertions, state["db"], assistant_texts)
        state["aurumdesk_breakdown"] = breakdown
        return float(breakdown["score"])

    return vf.Rubric(funcs=[aurumdesk_score], weights=[1.0])


# ---------- Module entrypoint (the verifiers convention) ----------

def load_environment(
    test_ids: list[str] | None = None,
    adversary_model: str = DEFAULT_ADVERSARY_MODEL,
    max_seller_tool_calls: int = 10,
    tasks_file: str = "tasks.json",
    seed_db_file: str = "seed_db.json",
    policy_file: str = "policy.md",
) -> vf.Environment:
    tasks_doc = json.loads((REPO / tasks_file).read_text())
    seed_db = json.loads((REPO / seed_db_file).read_text())
    policy = (REPO / policy_file).read_text()
    return AurumDeskNegotiationEnv(
        tasks_doc=tasks_doc,
        seed_db=seed_db,
        policy=policy,
        adversary_model=adversary_model,
        test_ids=test_ids,
        max_seller_tool_calls=max_seller_tool_calls,
    )
