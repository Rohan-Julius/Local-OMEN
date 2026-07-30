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
from typing import Callable, Literal, Protocol

import ollama
from pydantic import BaseModel

from omen import agents, tools as tools_mod
from omen.config import LLM_MODEL, NUM_CTX, NUM_GPU, TEMPERATURE, THINK_DEFAULT
from omen.contracts import Transcript
from omen.tools import ToolBudget


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


def _stopped_reason(budget: ToolBudget, steps: list, final_text: str) -> Literal["model_finished", "call_cap", "wall_clock_cap"]:
    if final_text:
        return "model_finished"
    if budget.elapsed_seconds() > budget.wall_clock_seconds:
        return "wall_clock_cap"
    if len(steps) >= budget.max_calls:
        return "call_cap"
    return "model_finished"


class ADKRoleRunner:
    """The default. Delegates all google.adk usage to agents.py."""

    async def run_structured(
        self, instruction: str, prompt: str, schema: type[BaseModel], think: bool = THINK_DEFAULT
    ) -> BaseModel:
        return await agents.run_structured_adk(instruction, prompt, schema, think)

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
        final_text = await agents.run_tooled_adk(
            instruction, prompt, wrapped, max_llm_calls=budget.max_calls + 4, think=think, on_text=on_text
        )
        return Transcript(steps=steps, final_text=final_text, stopped_reason=_stopped_reason(budget, steps, final_text))


class DirectOllamaRunner:
    """Ollama's `format` parameter constrains decoding to a JSON schema —
    a stronger guarantee than instruct-and-validate — and `/api/chat`
    returns `tool_calls` natively, so this needs no google.adk at all."""

    async def run_structured(
        self, instruction: str, prompt: str, schema: type[BaseModel], think: bool = THINK_DEFAULT
    ) -> BaseModel:
        def call():
            return ollama.chat(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": prompt},
                ],
                format=schema.model_json_schema(),
                think=think,
                options={"num_gpu": NUM_GPU, "num_ctx": NUM_CTX, "temperature": TEMPERATURE},
            )

        response = await asyncio.to_thread(call)
        return schema.model_validate_json(response.message.content)

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

        # Outer loop cap is a pure safety net above our own budget's cap
        # (which lives inside the wrapped tools) — mirrors ADKRoleRunner's
        # max_llm_calls belt-and-braces margin.
        for _ in range(budget.max_calls + 4):

            def call(msgs=list(messages)):
                return ollama.chat(
                    model=LLM_MODEL,
                    messages=msgs,
                    tools=wrapped,
                    think=think,
                    options={"num_gpu": NUM_GPU, "num_ctx": NUM_CTX, "temperature": TEMPERATURE},
                )

            response = await asyncio.to_thread(call)
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

        return Transcript(steps=steps, final_text=final_text, stopped_reason=_stopped_reason(budget, steps, final_text))
