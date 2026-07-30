"""Phase 6: RoleRunner contract tests, parametrized across both
ADKRoleRunner and DirectOllamaRunner — PLAN.md's reversibility hedge only
means something if both paths are actually exercised, not just one.

Live Ollama required (skips gracefully without it).
"""
import urllib.request

import pytest

from omen import agents, config, store, tools
from omen.contracts import TriageVerdict
from omen.runners import ADKRoleRunner, DirectOllamaRunner


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")

RUNNERS = [ADKRoleRunner, DirectOllamaRunner]

TRIAGE_INSTRUCTION = agents.load_prompt("triage.txt")
INVESTIGATOR_INSTRUCTION = agents.load_prompt("investigator.txt")

TRUE_POSITIVE_PROMPT = """CODE CHUNK (permissions.py:has_access, lines 3-5):
@functools.lru_cache(maxsize=1024)
def has_access(user_id, resource_id):
    return _compute_permission(user_id, resource_id)

CANDIDATE INCIDENTS:
OMEN-001: Stale permission cache after revocation
  failure_mechanism: An authorization decision is cached with no invalidation path tied to the permission-changing event, so a revoked permission keeps succeeding.
  the_rule: Any cache keyed on an authorization decision must be invalidated explicitly by the event that changes the permission.
"""

HARD_NEGATIVE_PROMPT = """CODE CHUNK (thumbnails.py:get_thumbnail, lines 1-7):
_thumb_cache = cachetools.TTLCache(maxsize=500, ttl=3600)

def get_thumbnail(image_id):
    if image_id in _thumb_cache:
        return _thumb_cache[image_id]
    thumb = render_thumbnail(image_id)
    _thumb_cache[image_id] = thumb
    return thumb

CANDIDATE INCIDENTS:
OMEN-001: Stale permission cache after revocation
  failure_mechanism: An authorization decision is cached with no invalidation path tied to the permission-changing event, so a revoked permission keeps succeeding.
  the_rule: Any cache keyed on an authorization decision must be invalidated explicitly by the event that changes the permission.
"""


@pytest.mark.parametrize("runner_cls", RUNNERS, ids=["adk", "direct"])
@pytest.mark.asyncio
async def test_run_structured_matches_true_positive(runner_cls):
    runner = runner_cls()
    verdict = await runner.run_structured(TRIAGE_INSTRUCTION, TRUE_POSITIVE_PROMPT, TriageVerdict, think=False)
    assert isinstance(verdict, TriageVerdict)
    assert verdict.verdict == "MATCH"


@pytest.mark.parametrize("runner_cls", RUNNERS, ids=["adk", "direct"])
@pytest.mark.asyncio
async def test_run_structured_rejects_hard_negative(runner_cls):
    runner = runner_cls()
    verdict = await runner.run_structured(TRIAGE_INSTRUCTION, HARD_NEGATIVE_PROMPT, TriageVerdict, think=False)
    assert verdict.verdict == "NO_MATCH"


@pytest.mark.parametrize("runner_cls", RUNNERS, ids=["adk", "direct"])
@pytest.mark.asyncio
async def test_run_structured_is_deterministic_on_verdict_and_confidence(runner_cls):
    runner = runner_cls()
    results = []
    for _ in range(3):
        v = await runner.run_structured(TRIAGE_INSTRUCTION, TRUE_POSITIVE_PROMPT, TriageVerdict, think=False)
        results.append((v.verdict, v.confidence))
    assert len(set(results)) == 1, f"verdict/confidence varied across runs: {results}"


@pytest.mark.parametrize("runner_cls", RUNNERS, ids=["adk", "direct"])
@pytest.mark.asyncio
async def test_run_tooled_produces_a_sensible_transcript(tmp_path, runner_cls):
    (tmp_path / "app.py").write_text(
        "def has_access(user_id, resource_id):\n"
        "    return _compute(user_id, resource_id)\n\n\n"
        "def _compute(user_id, resource_id):\n"
        "    return True\n"
    )
    conn = store.connect(tmp_path / "test.db")
    scan_tools = tools.build_scan_tools(tmp_path, conn)
    budget = tools.ToolBudget()

    prompt = (
        "Triage ruled MATCH against OMEN-001 (stale permission cache after revocation).\n\n"
        "CODE CHUNK (app.py:has_access, lines 1-2):\n"
        "def has_access(user_id, resource_id):\n"
        "    return _compute(user_id, resource_id)\n\n"
        "Investigate whether this chunk actually shares OMEN-001's failure mechanism."
    )

    runner = runner_cls()
    transcript = await runner.run_tooled(INVESTIGATOR_INSTRUCTION, prompt, scan_tools, budget, think=False)

    assert transcript.final_text != ""
    assert transcript.stopped_reason == "model_finished"
    assert len(transcript.steps) > 0
    # the model should actually have looked at the code, not just guessed
    tool_names_used = {s.tool_name for s in transcript.steps}
    assert tool_names_used & {"read_code", "grep_symbol", "get_incident", "search_memory"}


@pytest.mark.parametrize("runner_cls", RUNNERS, ids=["adk", "direct"])
@pytest.mark.asyncio
async def test_run_tooled_respects_a_tight_call_cap(tmp_path, runner_cls):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    conn = store.connect(tmp_path / "test.db")
    scan_tools = tools.build_scan_tools(tmp_path, conn)
    budget = tools.ToolBudget(max_calls=1, wall_clock_seconds=90)

    prompt = (
        "Read app.py lines 1-2 with read_code, then read it again with a different "
        "line range, then read it a third time, before answering."
    )
    runner = runner_cls()
    transcript = await runner.run_tooled(INVESTIGATOR_INSTRUCTION, prompt, scan_tools, budget, think=False)

    # The prompt adversarially insists on 3 attempts, so the model may well
    # *try* more than once after a cap message — that's a prompt-following
    # quirk, not a safety failure. What must hold: at most `max_calls`
    # attempts ever did real work; every attempt beyond that got nothing
    # but the cap notice, never a second real tool execution.
    real_calls = [s for s in transcript.steps if s.note is None]
    capped_calls = [s for s in transcript.steps if s.note and "exceeded" in s.note]
    assert len(real_calls) <= budget.max_calls
    assert len(real_calls) + len(capped_calls) == len(transcript.steps)
    assert all(s.tool_name in scan_tools for s in transcript.steps)
