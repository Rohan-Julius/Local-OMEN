"""Phase 5 (production plan), audit B5 part 2: separate time limits.

  a per-LLM-call timeout   (both runners)  -> stopped_reason="llm_timeout"
  a tool-time cap          (ToolBudget)    -> tool time only
  an overall deadline      (ToolBudget)    -> still stops a runaway loop

and the property the old single 90 s wall clock lacked: a slow model turn
between tool calls must NOT cause the next tool call to be refused.

No real model: slow fakes (a fake Ollama client that sleeps; a fake ADK
runner whose event stream sleeps).
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from types import SimpleNamespace

import pytest

from omen import agents, errors, profiles, runners
from omen.tools import ToolBudget


def use_profile(**overrides) -> profiles.Profile:
    profile = dataclasses.replace(profiles.GPU, **overrides)
    profiles.set_active(profile)  # conftest resets it after the test
    return profile


# ---------------------------------------------------------------------------
# fake Ollama client for the Direct runner
# ---------------------------------------------------------------------------


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


def slow_client(delay: float, tool_turns: int = 2, final="done"):
    """A client class whose every chat() takes `delay` seconds, asking for
    `tool_turns` distinct tool calls before answering."""

    class Client:
        def __init__(self, host=None, **kwargs):
            self.turns = 0

        def chat(self, **kwargs):
            time.sleep(delay)
            self.turns += 1
            if self.turns <= tool_turns:
                return _Resp(_Msg(tool_calls=[_ToolCall("ping", {"n": self.turns})]))
            return _Resp(_Msg(content=final))

    return Client


def ping(n: int) -> str:
    """Ping.

    Args:
        n: Which ping.
    """
    return f"pong {n}"


async def run_direct(monkeypatch, client_cls, budget):
    monkeypatch.setattr(runners.ollama, "Client", client_cls)
    return await runners.DirectOllamaRunner().run_tooled("instr", "prompt", {"ping": ping}, budget, think=False)


# ---------------------------------------------------------------------------
# (1) a slow model turn does not refuse the next tool call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_turn_between_tool_calls_does_not_refuse_the_next_tool_call(monkeypatch):
    use_profile(llm_call_timeout=5.0)
    # Every model turn takes 0.3 s: 3 turns = 0.9 s of generation, far above the
    # 0.2 s "tool time" cap. Under the old single wall clock (0.2 s) the second
    # tool call would have been refused.
    budget = ToolBudget(wall_clock_seconds=60, tool_seconds=0.2)

    transcript = await run_direct(monkeypatch, slow_client(0.3, tool_turns=2), budget)

    assert [s.note for s in transcript.steps] == [None, None]  # both tool calls ran
    assert transcript.final_text == "done"
    assert transcript.stopped_reason == "model_finished"


@pytest.mark.asyncio
async def test_slow_first_turn_does_not_refuse_the_first_tool_call(monkeypatch):
    use_profile(llm_call_timeout=5.0)
    transcript = await run_direct(monkeypatch, slow_client(0.4, tool_turns=1), ToolBudget(wall_clock_seconds=60, tool_seconds=0.1))
    assert [s.note for s in transcript.steps] == [None]
    assert transcript.stopped_reason == "model_finished"


def test_tool_calls_are_not_refused_however_long_the_model_took_before(monkeypatch):
    budget = ToolBudget(wall_clock_seconds=60, tool_seconds=0.2)
    tools = {"t": lambda x=0: "fine"}
    time.sleep(0.3)  # the model "thought" for longer than the tool cap before the first call
    assert budget.invoke(tools, "t", {}) == (True, "fine", None)
    time.sleep(0.3)  # and again between calls
    assert budget.invoke(tools, "t", {"x": 1}) == (True, "fine", None)


# ---------------------------------------------------------------------------
# (2) the tool-time cap
# ---------------------------------------------------------------------------


def test_tool_time_cap_counts_only_time_inside_tools():
    def slow_tool(n):
        time.sleep(0.15)
        return n

    budget = ToolBudget(wall_clock_seconds=60, tool_seconds=0.2)
    assert budget.invoke({"slow": slow_tool}, "slow", {"n": 1})[0] is True  # 0.15 s used
    assert budget.invoke({"slow": slow_tool}, "slow", {"n": 2})[0] is True  # 0.30 s used: over the cap now
    ok, result, note = budget.invoke({"slow": slow_tool}, "slow", {"n": 3})
    assert ok is False and "tool-time ceiling" in note
    assert budget.events == ["wall_clock_cap"]


# ---------------------------------------------------------------------------
# (3) the overall deadline still stops a loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overall_deadline_still_stops_a_runaway_loop(monkeypatch):
    use_profile(llm_call_timeout=5.0)
    budget = ToolBudget(wall_clock_seconds=0.25, tool_seconds=60)  # deadline counts model turns after the first call

    transcript = await run_direct(monkeypatch, slow_client(0.2, tool_turns=5, final="gave up"), budget)

    notes = [s.note for s in transcript.steps]
    assert notes[0] is None  # the first call is never charged for the first turn
    assert any(n and "wall-clock ceiling" in n for n in notes)
    assert transcript.stopped_reason == "wall_clock_cap"


# ---------------------------------------------------------------------------
# (4) per-LLM-call timeout: Direct runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_llm_call_timeout_ends_the_investigation_with_llm_timeout(monkeypatch):
    use_profile(llm_call_timeout=0.1)
    started = time.perf_counter()

    transcript = await run_direct(monkeypatch, slow_client(1.0), ToolBudget())

    assert time.perf_counter() - started < 0.9  # did not wait for the slow generation
    assert transcript.stopped_reason == "llm_timeout"
    assert transcript.steps == [] and transcript.final_text == ""
    assert "timed out after 0.1s" in transcript.note and "--profile" in transcript.note


@pytest.mark.asyncio
async def test_direct_llm_timeout_after_some_tool_calls_keeps_the_steps(monkeypatch):
    use_profile(llm_call_timeout=0.25)

    class Client:
        def __init__(self, host=None, **kwargs):
            self.turns = 0

        def chat(self, **kwargs):
            self.turns += 1
            if self.turns == 1:
                return _Resp(_Msg(tool_calls=[_ToolCall("ping", {"n": 1})]))
            time.sleep(1.0)  # the second generation hangs
            return _Resp(_Msg(content="never seen"))

    transcript = await run_direct(monkeypatch, Client, ToolBudget())

    assert [s.tool_name for s in transcript.steps] == ["ping"]
    assert transcript.stopped_reason == "llm_timeout"


@pytest.mark.asyncio
async def test_direct_structured_call_timeout_is_a_one_line_error(monkeypatch):
    from omen.contracts import TriageVerdict

    use_profile(llm_call_timeout=0.1)
    monkeypatch.setattr(runners.ollama, "Client", slow_client(1.0))
    with pytest.raises(errors.LLMTimeout) as exc:
        await runners.DirectOllamaRunner().run_structured("instr", "prompt", TriageVerdict)
    assert "\n" not in str(exc.value) and "timed out after 0.1s" in str(exc.value)


def test_direct_client_timeout_is_a_little_longer_than_the_call_timeout(monkeypatch):
    """The asyncio timeout fires first; the HTTP timeout then ends the worker
    thread so it does not keep running."""
    use_profile(llm_call_timeout=100.0)
    seen = []

    class Client:
        def __init__(self, host=None, timeout=None, **kwargs):
            seen.append(timeout)

    monkeypatch.setattr(runners.ollama, "Client", Client)
    runners.DirectOllamaRunner()._ollama()
    assert seen == [130.0]


# ---------------------------------------------------------------------------
# (5) per-LLM-call timeout: ADK runner
# ---------------------------------------------------------------------------


def _event(text="answer", thought=False):
    return SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=text, thought=thought)]))


def fake_adk(monkeypatch, script):
    """Replace ADK's InMemoryRunner with one whose event stream is the async
    generator function `script()`."""

    class FakeRunner:
        def __init__(self, agent=None, app_name=None):
            async def create_session(**kwargs):
                return SimpleNamespace(id="s1")

            self.session_service = SimpleNamespace(create_session=create_session)

        def run_async(self, **kwargs):
            return script()

    monkeypatch.setattr(agents, "InMemoryRunner", FakeRunner)


@pytest.mark.asyncio
async def test_adk_event_wait_timeout_reports_llm_timeout(monkeypatch):
    async def script():
        yield _event("first turn done")
        await asyncio.sleep(5)  # the next model turn hangs
        yield _event("never seen")

    fake_adk(monkeypatch, script)
    started = time.perf_counter()

    result = await agents.run_tooled_adk("instr", "prompt", [], max_llm_calls=10, think=False, llm_timeout=0.2)

    assert time.perf_counter() - started < 3
    assert result.llm_timed_out is True and result.hit_llm_call_limit is False
    assert result.final_text == "first turn done"


@pytest.mark.asyncio
async def test_adk_without_timeout_finishes_normally(monkeypatch):
    async def script():
        yield _event("thinking...", thought=True)
        yield _event("final answer")

    fake_adk(monkeypatch, script)
    result = await agents.run_tooled_adk("instr", "prompt", [], max_llm_calls=10, think=False, llm_timeout=5)
    assert result == agents.TooledResult("final answer", False, False)  # thoughts stay out of the answer


@pytest.mark.asyncio
async def test_adk_call_limit_is_still_reported(monkeypatch):
    async def script():
        yield _event("partial")
        raise agents.LlmCallsLimitExceededError("too many calls")

    fake_adk(monkeypatch, script)
    result = await agents.run_tooled_adk("instr", "prompt", [], max_llm_calls=2, think=False, llm_timeout=5)
    assert result == agents.TooledResult("partial", True, False)


@pytest.mark.asyncio
async def test_adk_runner_turns_a_timed_out_loop_into_an_llm_timeout_transcript(monkeypatch):
    profile = use_profile(llm_call_timeout=42.0)
    seen = {}

    async def fake_run_tooled_adk(instruction, prompt, tools, max_llm_calls, think, on_text, llm_timeout):
        seen["timeout"] = llm_timeout
        return agents.TooledResult("", False, True)

    monkeypatch.setattr(runners, "_agents", lambda: SimpleNamespace(run_tooled_adk=fake_run_tooled_adk))

    transcript = await runners.ADKRoleRunner().run_tooled("instr", "prompt", {"ping": ping}, ToolBudget(), think=False)

    assert seen["timeout"] == profile.llm_call_timeout == 42.0  # the profile's limit reaches ADK
    assert transcript.stopped_reason == "llm_timeout"
    assert "timed out after 42s" in transcript.note


@pytest.mark.asyncio
async def test_adk_structured_call_timeout_is_a_one_line_error(monkeypatch):
    from omen.contracts import TriageVerdict

    use_profile(llm_call_timeout=0.1)

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(runners, "_agents", lambda: SimpleNamespace(run_structured_adk=hang))
    started = time.perf_counter()
    with pytest.raises(errors.LLMTimeout) as exc:
        await runners.ADKRoleRunner().run_structured("instr", "prompt", TriageVerdict)
    assert time.perf_counter() - started < 3 and "timed out after 0.1s" in str(exc.value)


# ---------------------------------------------------------------------------
# defaults follow the active profile
# ---------------------------------------------------------------------------


def test_budget_defaults_come_from_the_active_profile(monkeypatch):
    profiles.set_active(profiles.CPU)
    cpu = ToolBudget()
    assert (cpu.wall_clock_seconds, cpu.tool_seconds) == (profiles.CPU.deadline_seconds, profiles.CPU.tool_seconds)
    profiles.set_active(profiles.GPU)
    gpu = ToolBudget()
    assert (gpu.wall_clock_seconds, gpu.tool_seconds) == (90.0, profiles.GPU.tool_seconds)
    explicit = ToolBudget(wall_clock_seconds=5, tool_seconds=6)
    assert (explicit.wall_clock_seconds, explicit.tool_seconds) == (5, 6)


def test_stopped_reason_prefers_the_first_cap_event():
    budget = ToolBudget()
    budget.record_event("llm_timeout")
    budget.record_event("call_cap")
    assert runners._stopped_reason(budget, [], "") == "llm_timeout"
