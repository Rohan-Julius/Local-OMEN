"""Phase 7 acceptance (PLAN.md):
- "Assert the assembled prompt contains neither Triage's `reasoning` nor
  its verdict — a string assertion, because a leak looks like success
  from the outside." (test_adjudicator_prompt_is_blind_to_triage)
- "Empty evidence_lines on a positive -> auto-downgraded in code."
  (test_confirmed_with_no_evidence_is_downgraded, no model needed)
- "fixture precision improves or holds vs phase 6" — Phase 6 was 4/4 true
  positives caught / 0/4 hard negatives flagged by Triage alone; Phase 7
  runs the same true-positive/hard-negative pair all the way through
  Investigator -> Adjudicator and checks the final verdict still holds.

Live Ollama required for the end-to-end test (skips gracefully without
it); the prompt-independence and downgrade tests are pure and always run.
"""
import urllib.request

import pytest

from omen import agents, cli, config, runners, store, tools
from omen.contracts import (
    AdjudicatorVerdict,
    Chunk,
    IncidentSeed,
    RetrievalCandidate,
    ToolCallStep,
    Transcript,
    TriageVerdict,
)


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


def test_confirmed_with_no_evidence_is_downgraded():
    v = AdjudicatorVerdict(
        verdict="confirmed", code_mechanism="x", reasoning="y", evidence_lines=[], confidence="high"
    )
    assert cli._finalize_adjudicator_verdict(v).verdict == "unverified"


def test_confirmed_with_evidence_is_not_downgraded():
    v = AdjudicatorVerdict(
        verdict="confirmed", code_mechanism="x", reasoning="y", evidence_lines=["app.py:3"], confidence="high"
    )
    assert cli._finalize_adjudicator_verdict(v).verdict == "confirmed"


def test_rejected_and_unverified_pass_through_unchanged():
    for verdict in ("rejected", "unverified"):
        v = AdjudicatorVerdict(
            verdict=verdict, code_mechanism="x", reasoning="y", evidence_lines=[], confidence="low"
        )
        assert cli._finalize_adjudicator_verdict(v).verdict == verdict


def test_adjudicator_prompt_is_blind_to_triage(tmp_path):
    """The Adjudicator prompt is built from the chunk, the candidates, and
    the investigation transcript alone — never from a TriageVerdict — so a
    distinctive Triage reasoning/verdict string must never appear in it."""
    conn = store.connect(tmp_path / "test.db")
    store.upsert_incident(
        conn,
        IncidentSeed(
            ref="OMEN-TEST",
            title="Test incident",
            failure_mechanism="things break",
            what_happened="it broke",
            the_rule="don't break it",
        ),
    )
    triage_verdict = TriageVerdict(
        code_mechanism="unrelated code_mechanism string",
        verdict="MATCH",
        confidence="high",
        reasoning="TRIAGE_SECRET_REASONING_MARKER should never leak into the adjudicator prompt",
    )
    transcript = Transcript(
        steps=[ToolCallStep(tool_name="read_code", args={"file_path": "app.py"}, result="def f(): pass")],
        final_text="Investigator's clean summary.",
        stopped_reason="model_finished",
    )
    chunk = Chunk(
        file_path="app.py", start_line=1, end_line=1, symbol="f", content="def f(): pass", content_hash="abc"
    )
    candidates = [RetrievalCandidate(incident_ref="OMEN-TEST", similarity=0.9, matched_variant="mechanism")]

    prompt = cli._format_adjudicator_prompt(conn, chunk, candidates, transcript)

    assert "TRIAGE_SECRET_REASONING_MARKER" not in prompt
    assert triage_verdict.reasoning not in prompt
    assert "MATCH" not in prompt  # Triage's literal verdict string
    # the transcript and candidate context should still be present
    assert "OMEN-TEST" in prompt
    assert "Investigator's clean summary." in prompt


pytestmark_live = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")

TRUE_POSITIVE_CODE = (
    "@functools.lru_cache(maxsize=1024)\n"
    "def has_access(user_id, resource_id):\n"
    "    return _compute_permission(user_id, resource_id)\n"
)
HARD_NEGATIVE_CODE = (
    "_thumb_cache = cachetools.TTLCache(maxsize=500, ttl=3600)\n\n"
    "def get_thumbnail(image_id):\n"
    "    if image_id in _thumb_cache:\n"
    "        return _thumb_cache[image_id]\n"
    "    thumb = render_thumbnail(image_id)\n"
    "    _thumb_cache[image_id] = thumb\n"
    "    return thumb\n"
)


@pytestmark_live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,symbol,should_confirm",
    [(TRUE_POSITIVE_CODE, "has_access", True), (HARD_NEGATIVE_CODE, "get_thumbnail", False)],
    ids=["true_positive", "hard_negative"],
)
async def test_adjudicator_end_to_end(tmp_path, code, symbol, should_confirm):
    repo = tmp_path / "repo"
    repo.mkdir()
    file_name = "permissions.py" if should_confirm else "thumbnails.py"
    (repo / file_name).write_text(code)

    conn = store.connect(tmp_path / "test.db")
    store.upsert_incident(
        conn,
        IncidentSeed(
            ref="OMEN-001",
            title="Stale permission cache after revocation",
            failure_mechanism=(
                "An authorization decision is cached with no invalidation path tied to the "
                "permission-changing event, so a revoked permission keeps succeeding."
            ),
            what_happened="A revoked permission kept succeeding because the cache was never invalidated.",
            the_rule="Any cache keyed on an authorization decision must be invalidated explicitly by the event that changes the permission.",
        ),
    )

    chunk = Chunk(
        file_path=file_name,
        start_line=1,
        end_line=code.count("\n") + 1,
        symbol=symbol,
        content=code,
        content_hash="test-hash",
    )
    candidates = [RetrievalCandidate(incident_ref="OMEN-001", similarity=0.9, matched_variant="mechanism")]

    runner = runners.ADKRoleRunner()
    triage_instruction = agents.load_prompt("triage.txt")
    investigator_instruction = agents.load_prompt("investigator.txt")
    adjudicator_instruction = agents.load_prompt("adjudicator.txt")

    triage_prompt = cli._format_triage_prompt(conn, chunk, candidates)
    triage_verdict = await runner.run_structured(triage_instruction, triage_prompt, TriageVerdict, think=False)

    if triage_verdict.verdict != "MATCH":
        # Triage already rejected it (a legitimate outcome for the hard
        # negative) — the Investigator/Adjudicator never run in the real
        # pipeline in that case either, so there's nothing further to check.
        assert not should_confirm
        return

    scan_tools = tools.build_scan_tools(repo, conn)
    budget = tools.ToolBudget()
    investigator_prompt = cli._format_investigator_prompt(chunk, triage_verdict, candidates)
    transcript = await runner.run_tooled(
        investigator_instruction, investigator_prompt, scan_tools, budget, think=False
    )

    adjudicator_prompt = cli._format_adjudicator_prompt(conn, chunk, candidates, transcript)
    raw_verdict = await runner.run_structured(
        adjudicator_instruction, adjudicator_prompt, AdjudicatorVerdict, think=False
    )
    verdict = cli._finalize_adjudicator_verdict(raw_verdict)

    if should_confirm:
        assert verdict.verdict == "confirmed", (
            f"expected confirmed, got {verdict.verdict}: {verdict.reasoning}"
        )
        assert verdict.evidence_lines, "confirmed verdict must carry evidence_lines (or be auto-downgraded)"
    else:
        assert verdict.verdict != "confirmed", f"hard negative was confirmed: {verdict.reasoning}"
