"""The reversibility hedge (PLAN.md): a strategy interface so role
execution is swappable between ADK and a direct Ollama client without
touching a prompt or a contract. `--runner=direct` is the one-line escape
if either ADK path (structured output or tool calling) proves unreliable
— it is not hypothetical insurance, both paths are fully implemented and
tested here.

This module never imports google.adk directly; ADKRoleRunner delegates to
agents.py, the sole importer, so the two runners stay genuinely
interchangeable at the import level too.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Literal, Protocol

import ollama
import pydantic
from pydantic import BaseModel

from omen import config, errors, profiles, tools as tools_mod
from omen.config import LLM_MODEL, NUM_CTX, NUM_GPU, TEMPERATURE, THINK_DEFAULT
from omen.contracts import Transcript
from omen.tools import ToolBudget

log = logging.getLogger("omen.runners")


class RoleRunner(Protocol):
    async def run_structured(
        self, instruction: str, prompt: str, schema: type[BaseModel], think: bool = THINK_DEFAULT
    ) -> BaseModel: ...

    async def run_tooled(
        self,
        instruction: str,
        prompt: str,
        tools: dict[str, Callable],
        budget: ToolBudget,
        think: bool = True,
        on_step: Callable | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> Transcript: ...


StoppedReason = Literal["model_finished", "call_cap", "wall_clock_cap", "error", "llm_timeout"]


def _stopped_reason(budget: ToolBudget, steps: list, final_text: str) -> StoppedReason:
    """Why the tool loop ended, from what actually happened (audit B13),
    not from whether final text exists: a capped loop that still wrote a
    summary was reported as "model_finished" before.

    - the first cap event (a call refused by the call cap, the overall
      deadline or the tool-time cap, a model turn that timed out, or the
      runner's outer safety net cutting the loop) wins;
    - otherwise "error" if a tool raised and the model gave no answer;
    - otherwise "model_finished".
    """
    for event in budget.events:
        if event in ("call_cap", "wall_clock_cap", "llm_timeout"):
            return event
    if not final_text and "error" in budget.events:
        return "error"
    return "model_finished"


def _llm_timeout() -> float:
    """Seconds one model generation may take: the active profile's limit
    (read per call, so a profile resolved after the runner was built applies)."""
    return profiles.active().llm_call_timeout


def _timeout_note(seconds: float) -> str:
    return str(errors.LLMTimeout(seconds, "an Investigator/Archivist model turn"))


async def _structured_with_retry(attempt: Callable[[], Awaitable[BaseModel]], schema: type[BaseModel]) -> BaseModel:
    """Run a structured call; if the reply does not parse into `schema`, try
    once more, then raise a one-line SchemaInvalid. Both runners go through
    here, so they behave the same."""
    first_error: Exception | None = None
    for n in (1, 2):
        try:
            return await attempt()
        except (pydantic.ValidationError, json.JSONDecodeError) as e:
            first_error = first_error or e
            log.warning("the model's reply did not match the %s schema (attempt %d of 2)", schema.__name__, n)
    detail = str(first_error).strip().splitlines()[0] if first_error else "unknown"
    raise errors.SchemaInvalid(f"the model's reply did not match the {schema.__name__} schema after 2 attempts ({detail})")


def _agents():
    """Import agents.py (and with it google.adk) only when an ADK role
    actually runs. Importing ADK costs seconds, and commands that never
    call a model should not pay for it (audit B28)."""
    from omen import agents

    return agents


class ADKRoleRunner:
    """The default. Delegates all google.adk usage to agents.py."""

    async def run_structured(
        self, instruction: str, prompt: str, schema: type[BaseModel], think: bool = THINK_DEFAULT
    ) -> BaseModel:
        timeout = _llm_timeout()

        async def attempt() -> BaseModel:
            try:
                return await asyncio.wait_for(_agents().run_structured_adk(instruction, prompt, schema, think), timeout)
            except (asyncio.TimeoutError, TimeoutError) as e:
                raise errors.LLMTimeout(timeout, "model call") from e
            except (pydantic.ValidationError, json.JSONDecodeError):
                raise  # a reply problem, retried by _structured_with_retry
            except Exception as e:
                raise (errors.classify(e, config.LLM_MODEL) or e) from e

        return await _structured_with_retry(attempt, schema)

    async def run_tooled(
        self,
        instruction: str,
        prompt: str,
        tools: dict[str, Callable],
        budget: ToolBudget,
        think: bool = True,
        on_step: Callable | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> Transcript:
        wrapped, steps = tools_mod.budgeted_tools(tools, budget, on_step=on_step)
        # ADK's own hard safety net, set a few calls above our budget's
        # cap: belt and braces in case the model ignores the budget's
        # stop-and-answer message entirely.
        timeout = _llm_timeout()
        try:
            result = await _agents().run_tooled_adk(
                instruction, prompt, wrapped, max_llm_calls=budget.max_calls + 4, think=think,
                on_text=on_text, llm_timeout=timeout,
            )
        except Exception as e:
            raise (errors.classify(e, config.LLM_MODEL) or e) from e
        note = None
        if result.hit_llm_call_limit:
            budget.record_event("call_cap")
        if result.llm_timed_out:
            budget.record_event("llm_timeout")
            note = _timeout_note(timeout)
        return Transcript(
            steps=steps, final_text=result.final_text,
            stopped_reason=_stopped_reason(budget, steps, result.final_text), note=note,
        )


class DirectOllamaRunner:
    """Ollama's `format` parameter constrains decoding to a JSON schema —
    a stronger guarantee than instruct-and-validate — and `/api/chat`
    returns `tool_calls` natively, so this needs no google.adk at all.

    The client is pointed at `config.OLLAMA_HOST` explicitly (B16), never
    the ollama library's own default.

    Every generation has a timeout (audit B5, part 2): `asyncio.wait_for`
    lets the loop move on, and the HTTP client's own timeout (a little
    longer) ends the worker thread soon after, so it does not run on."""

    def __init__(self) -> None:
        self._client: ollama.Client | None = None
        self._client_timeout: float | None = None

    def _ollama(self) -> ollama.Client:
        timeout = _llm_timeout() + 30.0
        if self._client is None or self._client_timeout != timeout:
            self._client = ollama.Client(host=config.OLLAMA_HOST, timeout=timeout)
            self._client_timeout = timeout
        return self._client

    async def _generate(self, call: Callable, timeout: float):
        """Run a blocking client call in a thread with a timeout; turn raw
        failures into the friendly errors (Ollama down, model missing)."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(call), timeout)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise errors.LLMTimeout(timeout, "model call") from e
        except (pydantic.ValidationError, json.JSONDecodeError):
            raise
        except Exception as e:
            raise (errors.classify(e, config.LLM_MODEL) or e) from e

    async def run_structured(
        self, instruction: str, prompt: str, schema: type[BaseModel], think: bool = THINK_DEFAULT
    ) -> BaseModel:
        timeout = _llm_timeout()

        def call():
            return self._ollama().chat(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": prompt},
                ],
                format=schema.model_json_schema(),
                think=think,
                options={"num_gpu": NUM_GPU, "num_ctx": NUM_CTX, "temperature": TEMPERATURE},
            )

        async def attempt() -> BaseModel:
            response = await self._generate(call, timeout)
            return schema.model_validate_json(response.message.content)

        return await _structured_with_retry(attempt, schema)

    async def run_tooled(
        self,
        instruction: str,
        prompt: str,
        tools: dict[str, Callable],
        budget: ToolBudget,
        think: bool = True,
        on_step: Callable | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> Transcript:
        wrapped, steps = tools_mod.budgeted_tools(tools, budget, on_step=on_step)
        by_name = {fn.__name__: fn for fn in wrapped}
        messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": prompt},
        ]
        final_text = ""
        note = None
        timeout = _llm_timeout()

        # Outer loop cap is a pure safety net above our own budget's cap
        # (which lives inside the wrapped tools) — mirrors ADKRoleRunner's
        # max_llm_calls belt-and-braces margin.
        for _ in range(budget.max_calls + 4):

            def call(msgs=list(messages)):
                return self._ollama().chat(
                    model=LLM_MODEL,
                    messages=msgs,
                    tools=wrapped,
                    think=think,
                    options={"num_gpu": NUM_GPU, "num_ctx": NUM_CTX, "temperature": TEMPERATURE},
                )

            try:
                response = await self._generate(call, timeout)
            except errors.LLMTimeout:
                budget.record_event("llm_timeout")
                note = _timeout_note(timeout)
                break
            msg = response.message

            text = msg.thinking or msg.content
            if text and on_text:
                on_text(text)

            if not msg.tool_calls:
                final_text = msg.content or ""
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                }
            )
            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                args = dict(tool_call.function.arguments)
                fn = by_name.get(name)
                result = fn(**args) if fn else f"error: unknown tool {name!r}"
                messages.append({"role": "tool", "tool_name": name, "content": str(result)})
        else:
            # The outer safety net ran out before the model stopped calling tools.
            budget.record_event("call_cap")

        return Transcript(
            steps=steps, final_text=final_text, stopped_reason=_stopped_reason(budget, steps, final_text), note=note
        )
