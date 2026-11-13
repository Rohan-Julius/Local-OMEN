"""ONLY module that imports google.adk (PLAN.md chokepoint table). Exposes
two generic execution functions, run_structured_adk and run_tooled_adk,
used by runners.py's ADKRoleRunner — which otherwise never touches the
framework directly, so `--runner=direct` can drop ADK from execution
without this file changing shape.
"""
from __future__ import annotations

import asyncio
import itertools
import os
from typing import Callable, NamedTuple

from pydantic import BaseModel

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
os.environ.pop("GOOGLE_API_KEY", None)

# The google.* imports must follow the two environment lines above.
from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.invocation_context import LlmCallsLimitExceededError  # noqa: E402
from google.adk.agents.run_config import RunConfig, StreamingMode  # noqa: E402
from google.adk.models.lite_llm import LiteLlm  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from omen import config  # noqa: E402
from omen.config import LLM_MODEL, NUM_CTX, NUM_GPU, TEMPERATURE, THINK_DEFAULT  # noqa: E402
from omen.prompts import load_prompt  # noqa: E402,F401 - kept for callers of agents.load_prompt

_APP_NAME = "omen"
_session_ids = itertools.count(1)


def _model(think: bool) -> LiteLlm:
    # PLAN.md "ollama_chat/, never ollama/" — and num_gpu/num_ctx/think are
    # the Phase 0 findings that are load-bearing for latency/correctness
    # on this GPU (see CLAUDE.md "Critical Ollama/GPU quirks"). api_base: the one
    # configured Ollama address (B16), not LiteLLM's built-in default.
    return LiteLlm(
        model=f"ollama_chat/{LLM_MODEL}",
        api_base=config.OLLAMA_HOST,
        num_ctx=NUM_CTX,
        num_gpu=NUM_GPU,
        think=think,
        temperature=TEMPERATURE,
    )


async def run_structured_adk(
    instruction: str, prompt: str, schema: type[BaseModel], think: bool = THINK_DEFAULT
) -> BaseModel:
    agent = LlmAgent(
        name="structured_role",
        model=_model(think),
        instruction=instruction,
        output_schema=schema,
        output_key="result",
        generate_content_config=types.GenerateContentConfig(temperature=TEMPERATURE),
    )
    runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)
    session_id = f"s{next(_session_ids)}"
    session = await runner.session_service.create_session(app_name=_APP_NAME, user_id=_APP_NAME, session_id=session_id)
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    async for _ in runner.run_async(user_id=_APP_NAME, session_id=session.id, new_message=content):
        pass
    session = await runner.session_service.get_session(app_name=_APP_NAME, user_id=_APP_NAME, session_id=session.id)
    raw = session.state.get("result")
    if isinstance(raw, schema):
        return raw
    return schema.model_validate(raw)


class TooledResult(NamedTuple):
    final_text: str
    hit_llm_call_limit: bool  # ADK's own max_llm_calls safety net cut the loop
    llm_timed_out: bool  # one model turn exceeded `llm_timeout`


async def run_tooled_adk(
    instruction: str,
    prompt: str,
    tools: list[Callable],
    max_llm_calls: int,
    think: bool = True,
    on_text: Callable[[str], None] | None = None,
    llm_timeout: float | None = None,
) -> TooledResult:
    """Drives the ADK agent loop to completion and returns its final
    free-text answer, whether ADK's own `max_llm_calls` safety net cut the
    loop (so the caller can report a call cap, audit B13), and whether a
    model turn timed out.

    Tool-call bookkeeping (caps/dedup/transcript) is already baked into
    `tools` by tools.budgeted_tools() before this is called — this
    function only drives ADK's side of the loop.

    `llm_timeout` (seconds, None = no limit) bounds the wait for each next
    event. ADK emits an event when a model response arrives and again when
    a tool result does, so the wait is one model turn plus, at most, one
    tool call (tools take milliseconds). On timeout the loop is cancelled
    and the result says so (audit B5, part 2). Cancelling is clean here:
    the waiting call is an awaited HTTP request, unlike the Direct
    runner's worker thread.

    `max_llm_calls` is ADK's own hard safety net, set by the caller a few
    calls above our own ToolBudget's cap — belt and braces, per PLAN.md,
    in case the model ignores our budget's stop-and-answer message
    entirely.
    """
    agent = LlmAgent(
        name="tooled_role",
        model=_model(think),
        instruction=instruction,
        tools=tools,
        generate_content_config=types.GenerateContentConfig(temperature=TEMPERATURE),
    )
    runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)
    session_id = f"s{next(_session_ids)}"
    session = await runner.session_service.create_session(app_name=_APP_NAME, user_id=_APP_NAME, session_id=session_id)
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    run_config = RunConfig(streaming_mode=StreamingMode.NONE, max_llm_calls=max_llm_calls)

    final_text_parts: list[str] = []
    hit_llm_call_limit = False
    llm_timed_out = False
    events = runner.run_async(user_id=_APP_NAME, session_id=session.id, new_message=content, run_config=run_config)
    try:
        while True:
            try:
                event = await asyncio.wait_for(events.__anext__(), llm_timeout)
            except StopAsyncIteration:
                break
            except (asyncio.TimeoutError, TimeoutError):
                llm_timed_out = True
                break
            except LlmCallsLimitExceededError:
                hit_llm_call_limit = True  # the outer safety net fired
                break
            if event.content and event.content.parts:
                for part in event.content.parts:
                    text = getattr(part, "text", None)
                    if not text:
                        continue
                    if on_text:
                        on_text(text)
                    # genai Part.thought marks live chain-of-thought (when
                    # think=True) — stream it, but keep it out of the
                    # returned final_text, which the Adjudicator (Phase 7)
                    # reads as the investigation's clean conclusion.
                    if not getattr(part, "thought", False):
                        final_text_parts.append(text)
    finally:
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # closing a cancelled generator can complain; the result is already decided
                pass

    return TooledResult("".join(final_text_parts).strip(), hit_llm_call_limit, llm_timed_out)
