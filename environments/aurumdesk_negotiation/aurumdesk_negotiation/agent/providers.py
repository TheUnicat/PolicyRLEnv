"""
Model-provider abstraction for the AurumDesk benchmark.

A Provider owns the conversation state with one model. It translates between
the canonical tool-schema format (currently OpenAI Responses-API style, kept
in agent/tools.py) and whatever the underlying API needs. It exposes one
high-level method, `send_user_message()`, which appends a user message,
runs the tool-call loop, and returns a TurnResult.

To add another provider (Anthropic, Google, Ollama, etc.): subclass Provider
and implement `send_user_message()`. The runner and checker don't change.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class TurnResult:
    """What one user-message turn produced — used by the runner to update state."""
    status: str                       # "complete" | "tool_call_cap_exceeded" | "error"
    tool_calls_used: int
    assistant_messages: list[str]     # text chunks emitted in this turn, in order
    tool_call_log: list[dict]         # { name, args, result } per tool call this turn
    trace_events: list[dict]          # provider-specific extra events, JSONL-friendly
    error: str | None = None


ToolDispatcher = Callable[[str, dict], dict]


class Provider(ABC):
    """Base class. Implementations own conversation state for one model."""
    name: str = "abstract"

    def __init__(
        self,
        model: str,
        system_prompt: str,
        tools: list[dict],
        max_tool_calls_per_turn: int = 10,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.max_tool_calls_per_turn = max_tool_calls_per_turn

    @abstractmethod
    def send_user_message(
        self,
        user_msg: str,
        dispatch_tool: ToolDispatcher,
    ) -> TurnResult:
        """Append a user message; loop tool calls until done or cap; return result."""


class OpenAIProvider(Provider):
    """OpenAI Responses-API implementation. Reads OPENAI_API_KEY from env."""
    name = "openai"

    def __init__(
        self,
        model: str,
        system_prompt: str,
        tools: list[dict],
        max_tool_calls_per_turn: int = 10,
    ):
        super().__init__(model, system_prompt, tools, max_tool_calls_per_turn)
        from openai import OpenAI

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.client = OpenAI()
        # Responses-API contract: re-send the full input list each turn.
        self.input_items: list[dict] = [
            {"role": "system", "content": system_prompt}
        ]

    def send_user_message(self, user_msg: str, dispatch_tool: ToolDispatcher) -> TurnResult:
        self.input_items.append({"role": "user", "content": user_msg})
        events: list[dict] = [{"t": time.time(), "event": "user_message", "content": user_msg}]
        assistant_msgs: list[str] = []
        tool_calls: list[dict] = []
        tool_calls_used = 0

        for _ in range(self.max_tool_calls_per_turn + 1):
            try:
                response = self._call_api(events)
            except Exception as e:  # noqa: BLE001
                events.append({"t": time.time(), "event": "api_error", "error": str(e)})
                return TurnResult(
                    status="error",
                    tool_calls_used=tool_calls_used,
                    assistant_messages=assistant_msgs,
                    tool_call_log=tool_calls,
                    trace_events=events,
                    error=str(e),
                )

            had_tool_calls = False
            for item in response.output:
                d = self._as_dict(item)
                t = d.get("type")

                if t == "function_call":
                    had_tool_calls = True
                    tool_calls_used += 1
                    name = d["name"]
                    try:
                        args = json.loads(d.get("arguments") or "{}")
                    except json.JSONDecodeError as e:
                        events.append({
                            "t": time.time(),
                            "event": "tool_call_arg_parse_error",
                            "tool": name,
                            "raw_arguments": d.get("arguments"),
                            "error": str(e),
                        })
                        args = {}

                    result = dispatch_tool(name, args)
                    self.input_items.append(self._strip_output_only(d))
                    self.input_items.append({
                        "type": "function_call_output",
                        "call_id": d["call_id"],
                        "output": json.dumps(result),
                    })
                    tool_calls.append({"name": name, "args": args, "result": result})
                    events.append({
                        "t": time.time(),
                        "event": "tool_call",
                        "name": name,
                        "args": args,
                        "result": result,
                    })

                elif t == "message":
                    text = self._extract_text(d)
                    if text:
                        assistant_msgs.append(text)
                        events.append({"t": time.time(), "event": "assistant_message", "content": text})
                    self.input_items.append(self._strip_output_only(d))

                else:
                    # reasoning, refusal, custom tool calls, etc. — preserve for context
                    self.input_items.append(self._strip_output_only(d))
                    events.append({"t": time.time(), "event": "other_item", "type": t})

            if not had_tool_calls:
                return TurnResult(
                    status="complete",
                    tool_calls_used=tool_calls_used,
                    assistant_messages=assistant_msgs,
                    tool_call_log=tool_calls,
                    trace_events=events,
                )

            if tool_calls_used >= self.max_tool_calls_per_turn:
                events.append({"t": time.time(), "event": "tool_call_cap_exceeded"})
                return TurnResult(
                    status="tool_call_cap_exceeded",
                    tool_calls_used=tool_calls_used,
                    assistant_messages=assistant_msgs,
                    tool_call_log=tool_calls,
                    trace_events=events,
                )

        return TurnResult(
            status="tool_call_cap_exceeded",
            tool_calls_used=tool_calls_used,
            assistant_messages=assistant_msgs,
            tool_call_log=tool_calls,
            trace_events=events,
        )

    def _call_api(self, events: list[dict]):
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                return self.client.responses.create(
                    model=self.model,
                    input=self.input_items,
                    tools=self.tools,
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e).lower()
                if any(s in msg for s in ("rate limit", "timeout", "503", "502", "504")):
                    events.append({
                        "t": time.time(),
                        "event": "transient_error",
                        "attempt": attempt + 1,
                        "error": str(e),
                    })
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        assert last_err is not None
        raise last_err

    @staticmethod
    def _as_dict(item: Any) -> dict:
        if hasattr(item, "model_dump"):
            return item.model_dump()
        if isinstance(item, dict):
            return item
        return json.loads(json.dumps(item, default=str))

    # Fields that the Responses API returns on output but rejects when re-fed
    # as input. gpt-5.5 in particular rejects items with `status`. Newer models
    # may add more fields here over time.
    _OUTPUT_ONLY_KEYS = ("status",)

    @classmethod
    def _strip_output_only(cls, d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in cls._OUTPUT_ONLY_KEYS}

    @staticmethod
    def _extract_text(item: dict) -> str:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                parts.append(part.get("text", ""))
        return "".join(parts)
