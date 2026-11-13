"""Phase 3 (production plan), audit B13: `stopped_reason` comes from what
happened in the tool loop (cap events, tool errors), not from whether the
model wrote final text. No model: the Ollama client is faked.
"""
from __future__ import annotations

import pytest

from omen import runners
from omen.tools import ToolBudget


def _budget_with(*events: str) -> ToolBudget:
    budget = ToolBudget()
    for e in events:
        budget.record_event(e)
    return budget


@pytest.mark.parametrize(
    "events,final_text,expected",
    [
        ((), "done", "model_finished"),
        ((), "", "model_finished"),
        (("call_cap",), "a summary written after the cap", "call_cap"),  # was model_finished
        (("wall_clock_cap",), "a summary written after the cap", "wall_clock_cap"),  # was model_finished
        (("error", "wall_clock_cap", "call_cap"), "", "wall_clock_cap"),  # first cap wins
        (("error",), "", "error"),
        (("error",), "the model recovered and answered", "model_finished"),
    ],
)
def test_stopped_reason_from_events(events, final_text, expected):
    assert runners._stopped_reason(_budget_with(*events), [], final_text) == expected


def test_budget_records_cap_and_error_events():
    budget = ToolBudget(max_calls=1, wall_clock_seconds=90)
    tools = {"ok": lambda: "fine", "boom": lambda: 1 / 0}
    budget.invoke(tools, "boom", {})
    budget.invoke(tools, "ok", {})  # refused: call cap
    assert budget.events == ["error", "call_cap"]


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, name, arguments):
        self.function = _Fn(name, arguments)

    def model_dump(self):
        return {"function": {"name": self.function.name, "arguments": self.function.arguments}}


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content, self.tool_calls, self.thinking = content, tool_calls, None


class _Resp:
    def __init__(self, msg):
        self.message = msg


class FakeClient:
    """Asks for `ping` three times (different arguments, so no duplicate
    hits), then answers."""

    def __init__(self, host=None, **kwargs):
        self.turns = 0

    def chat(self, **kwargs):
        self.turns += 1
        if self.turns <= 3:
            return _Resp(_Msg(tool_calls=[_ToolCall("ping", {"n": self.turns})]))
        return _Resp(_Msg(content="final answer after the cap"))


@pytest.mark.asyncio
async def test_direct_runner_reports_call_cap_even_with_final_text(monkeypatch):
    monkeypatch.setattr(runners.ollama, "Client", FakeClient)

    def ping(n: int) -> str:
        """Ping.

        Args:
            n: Which ping.
        """
        return f"pong {n}"

    transcript = await runners.DirectOllamaRunner().run_tooled(
        "instr", "prompt", {"ping": ping}, ToolBudget(max_calls=2, wall_clock_seconds=90), think=False
    )
    assert transcript.final_text == "final answer after the cap"
    assert [s.note for s in transcript.steps][:2] == [None, None]
    assert "exceeded" in transcript.steps[2].note
    assert transcript.stopped_reason == "call_cap"
