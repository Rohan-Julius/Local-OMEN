"""ONLY module that imports google.adk (PLAN.md chokepoint table). Exposes
two generic execution functions, run_structured_adk and run_tooled_adk,
used by runners.py's ADKRoleRunner — which otherwise never touches the
framework directly, so `--runner=direct` can drop ADK from execution
without this file changing shape.
"""
from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
os.environ.pop("GOOGLE_API_KEY", None)

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.genai import types

from omen.config import LLM_MODEL, NUM_CTX, NUM_GPU, TEMPERATURE, THINK_DEFAULT

PROMPTS_DIR = Path("prompts")
_APP_NAME = "omen"
_session_ids = itertools.count(1)


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _model(think: bool) -> LiteLlm:
    # PLAN.md "ollama_chat/, never ollama/" — and num_gpu/num_ctx/think are
    # the Phase 0 findings that are load-bearing for latency/correctness
    # on this GPU (see README "Phase 0 results").
    return LiteLlm(model=f"ollama_chat/{LLM_MODEL}", num_ctx=NUM_CTX, num_gpu=NUM_GPU, think=think, temperature=TEMPERATURE)


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


async def run_tooled_adk(
    instruction: str,
    prompt: str,
    tools: list[Callable],
    max_llm_calls: int,
    think: bool = True,
    on_text: Callable[[str], None] | None = None,
) -> str:
    """Drives the ADK agent loop to completion and returns its final
    free-text answer. Tool-call bookkeeping (caps/dedup/transcript) is
    already baked into `tools` by tools.budgeted_tools() before this is
    called — this function only drives ADK's side of the loop.

    `max_llm_calls` is ADK's own hard safety net, set by the caller a few
    calls above our own ToolBudget's cap — belt and braces, per PLAN.md,
    in case the model ignores our budget's stop-and-answer message
    entirely (Phase 0 showed this can happen with an unsatisfying tool
    response; it should not happen once the response is a clear
    instruction, but the outer net stays regardless).
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
    try:
        async for event in runner.run_async(
            user_id=_APP_NAME, session_id=session.id, new_message=content, run_config=run_config
        ):
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
    except LlmCallsLimitExceededError:
        pass  # the outer safety net fired; caller infers stopped_reason from the (likely empty) result

    return "".join(final_text_parts).strip()
