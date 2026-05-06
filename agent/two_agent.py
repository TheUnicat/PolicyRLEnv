"""
TwoAgentRunner — drive a conversation between two Provider instances.

Both sides are independently configurable: each side has its own Provider
(model, system prompt, tools) and its own tool dispatcher. The runner does
not assume which side has which role; conventionally the "agent" side has
tools that mutate a domain DB and the "adversary" side has only the give_up
tool, but the runner enforces neither.

The runner does NOT evaluate the conversation. It just runs it and stops on
a termination condition. The caller is responsible for grading (typically
via checker/check.py against the post-run DB and the agent's user-facing
messages).

Termination conditions:
    - max_rounds reached. One round = one agent reply followed by one
      adversary reply, except the final round skips the trailing adversary
      turn (since the conversation should end on the agent's word).
    - Either side calls a tool literally named "give_up". Detected by
      scanning each turn's tool_call_log; the dispatcher only needs to ack.
    - Either side returns no text on its turn (silent → end).
    - Hard provider error.

Conversation seeding:
    The adversary speaks first, prompted by `scenario_cue` (a single user
    message sent into the adversary's Provider). The agent then receives
    that text as ITS user message; its reply becomes the adversary's next
    user message; and so on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .providers import Provider, ToolDispatcher, TurnResult


# ---------- Result type ----------

@dataclass
class TwoAgentResult:
    agent_messages: list[str] = field(default_factory=list)        # agent-side user-facing text (input to grading)
    adversary_messages: list[str] = field(default_factory=list)    # adversary-side text (audit only)
    trace: list[dict] = field(default_factory=list)                # unified trace; every event tagged with side + turn
    turn_summaries: list[dict] = field(default_factory=list)       # per-turn status, tool_calls_used, side
    outcome: str = "turn_cap"                                      # "turn_cap" | "give_up_<label>" | "agent_silent" | "adversary_silent" | "error"
    outcome_detail: dict | None = None                             # side-specific extras (give_up payload, error message, etc.)


# ---------- Runner ----------

class TwoAgentRunner:
    """
    Alternating-turn driver. Both providers are passed in fully configured
    (model, system prompt, tools, max_tool_calls_per_turn handled internally).
    Both dispatchers are passed in too — the runner doesn't know what either
    side's tools do.

    The runner inspects each turn's tool_call_log for any entry named
    "give_up" to detect early termination; it doesn't otherwise interpret
    tool calls.
    """

    def __init__(
        self,
        agent_provider: Provider,
        agent_dispatcher: ToolDispatcher,
        adversary_provider: Provider,
        adversary_dispatcher: ToolDispatcher,
        scenario_cue: str = "[Begin the conversation. You speak first.]",
        max_rounds: int = 6,
    ):
        self.agent_provider = agent_provider
        self.agent_dispatcher = agent_dispatcher
        self.adversary_provider = adversary_provider
        self.adversary_dispatcher = adversary_dispatcher
        self.scenario_cue = scenario_cue
        self.max_rounds = max_rounds
        self.result = TwoAgentResult()

    # ---------- internal helpers ----------

    def _record(self, side: str, turn_num: int, tr: TurnResult) -> None:
        for ev in tr.trace_events:
            self.result.trace.append({**ev, "side": side, "turn": turn_num})
        if side == "agent":
            self.result.agent_messages.extend(tr.assistant_messages)
        else:
            self.result.adversary_messages.extend(tr.assistant_messages)
        self.result.turn_summaries.append({
            "turn": turn_num,
            "side": side,
            "status": tr.status,
            "tool_calls_used": tr.tool_calls_used,
            "error": tr.error,
        })

    @staticmethod
    def _give_up_args(tr: TurnResult) -> dict | None:
        for tc in tr.tool_call_log:
            if tc["name"] == "give_up":
                return tc["args"]
        return None

    @staticmethod
    def _joined_text(tr: TurnResult) -> str:
        return " ".join(m for m in tr.assistant_messages if m).strip()

    def _step(self, side: str, turn_num: int, input_text: str) -> tuple[TurnResult, str, dict | None]:
        if side == "agent":
            tr = self.agent_provider.send_user_message(input_text, self.agent_dispatcher)
        else:
            tr = self.adversary_provider.send_user_message(input_text, self.adversary_dispatcher)
        self._record(side, turn_num, tr)
        return tr, self._joined_text(tr), self._give_up_args(tr)

    def _terminate_error(self, side: str, tr: TurnResult) -> TwoAgentResult:
        self.result.outcome = "error"
        self.result.outcome_detail = {"side": side, "error": tr.error}
        return self.result

    def _terminate_silent(self, side: str) -> TwoAgentResult:
        self.result.outcome = f"{side}_silent"
        return self.result

    def _terminate_give_up(self, side: str, payload: dict) -> TwoAgentResult:
        outcome_label = payload.get("outcome", "other")
        self.result.outcome = f"give_up_{outcome_label}"
        self.result.outcome_detail = {"side": side, **payload}
        return self.result

    # ---------- main loop ----------

    def run(self) -> TwoAgentResult:
        # --- Round 0: adversary opens, prompted by the scenario cue ---
        adv_tr, adv_text, give_up = self._step("adversary", 1, self.scenario_cue)
        if adv_tr.status == "error":
            return self._terminate_error("adversary", adv_tr)
        if give_up is not None:
            # give_up takes precedence over silent: the model may call give_up
            # with no parting text, which is a valid termination signal.
            return self._terminate_give_up("adversary", give_up)
        if not adv_text:
            return self._terminate_silent("adversary")

        turn_num = 2
        for round_idx in range(1, self.max_rounds + 1):
            # --- agent turn ---
            agent_tr, agent_text, give_up = self._step("agent", turn_num, adv_text)
            turn_num += 1
            if agent_tr.status == "error":
                return self._terminate_error("agent", agent_tr)
            if give_up is not None:
                return self._terminate_give_up("agent", give_up)
            if not agent_text:
                return self._terminate_silent("agent")

            # End-on-agent: don't ask the adversary another turn after the final round.
            if round_idx >= self.max_rounds:
                break

            # --- adversary turn ---
            adv_tr, adv_text, give_up = self._step("adversary", turn_num, agent_text)
            turn_num += 1
            if adv_tr.status == "error":
                return self._terminate_error("adversary", adv_tr)
            if give_up is not None:
                return self._terminate_give_up("adversary", give_up)
            if not adv_text:
                return self._terminate_silent("adversary")

        # Reached max_rounds without early termination. outcome stays "turn_cap".
        return self.result
