"""
AgentRunner — provider-agnostic test runner.

Owns the mutable DB. Dispatches tool calls into agent.tools.dispatch.
Delegates conversation/API work to whatever Provider you pass in.

Swap the provider, and nothing about reward / checking / DB state changes.
"""

from __future__ import annotations

from . import tools as agent_tools
from .providers import Provider, TurnResult


class AgentRunner:
    def __init__(self, provider: Provider, db: dict):
        self.provider = provider
        self.db = db
        self.assistant_messages: list[str] = []
        self.tool_call_log: list[dict] = []
        self.trace: list[dict] = []
        self.turn_summaries: list[dict] = []

    def _dispatch(self, name: str, args: dict) -> dict:
        return agent_tools.dispatch(self.db, name, args)

    def run_one_message(self, user_msg: str) -> TurnResult:
        """Send a single user message; return the TurnResult. Caller can print/log per-turn."""
        self.trace.append({"event": "turn_start", "turn": len(self.turn_summaries) + 1})
        result = self.provider.send_user_message(user_msg, self._dispatch)
        self.assistant_messages.extend(result.assistant_messages)
        self.tool_call_log.extend(result.tool_call_log)
        self.trace.extend(result.trace_events)
        self.turn_summaries.append({
            "turn": len(self.turn_summaries) + 1,
            "status": result.status,
            "tool_calls_used": result.tool_calls_used,
            "error": result.error,
        })
        return result

    def run_user_messages(self, user_messages: list[str]) -> None:
        """Run the full multi-turn script. Stops early on a hard provider error."""
        for msg in user_messages:
            r = self.run_one_message(msg)
            if r.status == "error":
                break
