"""Phase 7 acceptance (PLAN.md), extended in production-plan Phase 3:
- "Assert the assembled prompt contains neither Triage's `reasoning` nor
  its verdict — a string assertion, because a leak looks like success
  from the outside." Since Phase 3 (audit B7) this covers the system
  instructions of both the Adjudicator and the Investigator, and the
  Investigator's prompt, not only the Adjudicator's prompt.
- Evidence validation (audit B6) is tested in tests/test_evidence.py; it
  replaced the old "non-empty evidence_lines" rule.
- The per-call Adjudicator schema (audit B17): `incident_ref` is limited to
  the chunk's candidate refs.
- "fixture precision improves or holds vs phase 6": the same true-positive/
  hard-negative pair all the way through Investigator -> Adjudicator.

Live Ollama required for the end-to-end and live structured tests (they
skip without it); everything else is pure and always runs.
"""

import pydantic
import pytest

from omen import cli, evidence, prompts, runners, store, tools
from omen.contracts import (
    AdjudicatorVerdict,
    Chunk,
    IncidentSeed,
    RetrievalCandidate,
    ToolCallStep,
    Transcript,
    TriageVerdict,
    adjudicator_schema,
)


# ---------------------------------------------------------------------------
# B17: the per-call schema
# ---------------------------------------------------------------------------


def test_adjudicator_schema_limits_incident_ref_to_the_candidates():
    schema = adjudicator_schema(["OMEN-001", "OMEN-004", "OMEN-001"])
    base = {"verdict": "rejected", "code_mechanism": "m", "reasoning": "r", "confidence": "low"}

    assert schema.model_validate({**base, "incident_ref": "OMEN-004"}).incident_ref == "OMEN-004"
    with pytest.raises(pydantic.ValidationError):
        schema.model_validate({**base, "incident_ref": "OMEN-002"})  # not a candidate
    with pytest.raises(pydantic.ValidationError):
        schema.model_validate(base)  # required in the per-call schema
    assert issubclass(schema, AdjudicatorVerdict)


@pytest.mark.parametrize("refs", [["OMEN-001"], ["OMEN-001", "OMEN-004"]])
def test_adjudicator_schema_json_has_an_enum_for_both_runners(refs):
    """Both runners pass this JSON schema to the model (ADK output_schema,
    Ollama format=). A single ref must still be an enum, not only const."""
    js = adjudicator_schema(refs).model_json_schema()
    assert js["properties"]["incident_ref"]["enum"] == refs
    assert "incident_ref" in js["required"]
    assert list(js["properties"])[:5] == ["verdict", "code_mechanism", "reasoning", "evidence_lines", "confidence"]


def test_adjudicator_schema_needs_a_candidate():
    with pytest.raises(ValueError):
        adjudicator_schema([])


# ---------------------------------------------------------------------------
# B7: blindness to Triage
# ---------------------------------------------------------------------------


def test_role_instructions_do_not_mention_triage():
    """The Adjudicator's instruction used to say "Triage already ruled this
    chunk a plausible match"; the Investigator's said Triage "flagged" it."""
    for name in ("adjudicator.txt", "investigator.txt"):
        assert "triage" not in prompts.load_prompt(name).lower(), name


def test_investigator_prompt_is_built_without_triage(tmp_path):
    conn = store.connect(tmp_path / "test.db")
    store.upsert_incident(
        conn,
        IncidentSeed(ref="OMEN-TEST", title="Test incident", failure_mechanism="things break", what_happened="x", the_rule="y"),
    )
    chunk = Chunk(file_path="app.py", start_line=1, end_line=1, symbol="f", content="def f(): pass", content_hash="abc")
    candidates = [RetrievalCandidate(incident_ref="OMEN-TEST", similarity=0.9, matched_variant="mechanism")]

    prompt = cli._format_investigator_prompt(conn, chunk, candidates)

    assert "Triage" not in prompt and "MATCH" not in prompt
    assert "def f(): pass" in prompt
    assert "OMEN-TEST: Test incident" in prompt and "things break" in prompt


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


@pytest.mark.live
@pytest.mark.slow
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
    triage_instruction = prompts.load_prompt("triage.txt")
    investigator_instruction = prompts.load_prompt("investigator.txt")
    adjudicator_instruction = prompts.load_prompt("adjudicator.txt")

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
    investigator_prompt = cli._format_investigator_prompt(conn, chunk, candidates)
    transcript = await runner.run_tooled(
        investigator_instruction, investigator_prompt, scan_tools, budget, think=False
    )
    print(f"\ninvestigator: stopped_reason={transcript.stopped_reason}")
    for step in transcript.steps:
        print(f"  [tool] {step.tool_name}({step.args}) note={step.note!r} -> {step.result[:80]!r}")

    adjudicator_prompt = cli._format_adjudicator_prompt(conn, chunk, candidates, transcript)
    raw_verdict = await runner.run_structured(
        adjudicator_instruction, adjudicator_prompt, adjudicator_schema(["OMEN-001"]), think=False
    )
    verdict, downgrade_reason = evidence.validate_confirmed(raw_verdict, chunk, transcript, repo)
    print(f"adjudicator: raw={raw_verdict.verdict} final={verdict.verdict} ref={verdict.incident_ref} "
          f"evidence={verdict.evidence_lines} downgrade={downgrade_reason!r}")
    assert verdict.incident_ref == "OMEN-001"

    if should_confirm:
        assert verdict.verdict == "confirmed", (
            f"expected confirmed, got {verdict.verdict} (downgrade: {downgrade_reason}): {verdict.reasoning}"
        )
        assert verdict.evidence_lines, "confirmed verdict must carry evidence_lines (or be auto-downgraded)"
    else:
        assert verdict.verdict != "confirmed", f"hard negative was confirmed: {verdict.reasoning}"


# ---------------------------------------------------------------------------
# B17 live: one structured Adjudicator call per runner
# ---------------------------------------------------------------------------

LIVE_ADJUDICATOR_PROMPT = """CODE CHUNK (permissions.py:has_access, lines 3-5):
@functools.lru_cache(maxsize=1024)
def has_access(user_id, resource_id):
    return _compute_permission(user_id, resource_id)

CANDIDATE INCIDENTS:
OMEN-004: Check-then-act race condition without atomicity
  failure_mechanism: A value is checked and then acted on in two separate steps with no lock or atomic operation between them, so a concurrent change lands in the gap.
  the_rule: Any check-then-act sequence on shared state must be atomic.
OMEN-001: Stale permission cache after revocation
  failure_mechanism: An authorization decision is cached with no invalidation path tied to the permission-changing event, so a revoked permission keeps succeeding.
  the_rule: Any cache keyed on an authorization decision must be invalidated explicitly by the event that changes the permission.

INVESTIGATION TRANSCRIPT:
  [1] read_code({'file_path': 'permissions.py', 'start_line': 1, 'end_line': 12}) -> import functools

@functools.lru_cache(maxsize=1024)
def has_access(user_id, resource_id):
    return _compute_permission(user_id, resource_id)


def revoke(user_id, resource_id):
    _store.delete(user_id, resource_id)
  [2] grep_symbol({'symbol': 'cache_clear'}) -> []

INVESTIGATOR'S SUMMARY:
has_access caches the permission decision with functools.lru_cache (permissions.py:3-5). revoke (permissions.py:8-9) deletes the stored permission but never calls has_access.cache_clear(); grep_symbol found no cache_clear call anywhere.
"""


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("runner_cls", [runners.ADKRoleRunner, runners.DirectOllamaRunner], ids=["adk", "direct"])
async def test_live_structured_adjudicator_returns_a_candidate_ref(runner_cls):
    """The candidates are listed with the less relevant one (OMEN-004)
    first, so recording against candidates[0] would be wrong."""
    schema = adjudicator_schema(["OMEN-004", "OMEN-001"])
    verdict = await runner_cls().run_structured(
        prompts.load_prompt("adjudicator.txt"), LIVE_ADJUDICATOR_PROMPT, schema, think=False
    )
    print(f"\n{runner_cls.__name__}: verdict={verdict.verdict} incident_ref={verdict.incident_ref} "
          f"confidence={verdict.confidence} evidence={verdict.evidence_lines}")
    assert isinstance(verdict, schema)
    assert verdict.incident_ref in ("OMEN-004", "OMEN-001")
    assert verdict.incident_ref == "OMEN-001", f"chose {verdict.incident_ref}: {verdict.reasoning}"


# ---------------------------------------------------------------------------
# Phase 6 (live, NOT run in Phase 6): the Adjudicator on a hard negative
# ---------------------------------------------------------------------------
#
# `test_adjudicator_end_to_end[hard_negative]` returns early whenever Triage
# rejects the chunk, so on its own it never shows what the Adjudicator does
# with a hard negative. These tests skip Triage and the Investigator: they
# hand the Adjudicator a hard-negative chunk and an investigation transcript
# that already contains the evidence the chunk is safe, and assert it does not
# confirm. Both chunks come from fixtures/calibration.yaml (HN1 and HN4); the
# transcripts are written here, as a real Investigator would plausibly produce.

HN_THUMBNAIL_PROMPT = """CODE CHUNK (thumbnails.py:get_thumbnail, lines 1-9):
_thumb_cache = cachetools.TTLCache(maxsize=500, ttl=3600)

def get_thumbnail(image_id):
    if image_id in _thumb_cache:
        return _thumb_cache[image_id]
    thumb = render_thumbnail(image_id)
    _thumb_cache[image_id] = thumb
    return thumb

CANDIDATE INCIDENTS:
OMEN-001: Stale permission cache after revocation
  failure_mechanism: An authorization decision is cached with no invalidation path tied to the permission-changing event, so a revoked or downgraded permission keeps succeeding until the cache entry expires on its own or the process restarts.
  the_rule: Any cache keyed on an authorization decision must be invalidated explicitly by the event that changes the permission (role update, revocation, deactivation) - a TTL alone is not sufficient.

INVESTIGATION TRANSCRIPT:
  [1] read_code({'file_path': 'thumbnails.py', 'start_line': 1, 'end_line': 30}) -> _thumb_cache = cachetools.TTLCache(maxsize=500, ttl=3600)

def get_thumbnail(image_id):
    if image_id in _thumb_cache:
        return _thumb_cache[image_id]
    thumb = render_thumbnail(image_id)
    _thumb_cache[image_id] = thumb
    return thumb
  [2] grep_symbol({'symbol': 'permission'}) -> []
  [3] grep_symbol({'symbol': 'get_thumbnail'}) -> ['gallery.py:14: <img src=thumbnail_url(get_thumbnail(photo.id))>']

INVESTIGATOR'S SUMMARY:
get_thumbnail (thumbnails.py:3-8) caches a rendered image thumbnail keyed by image id in a TTLCache. Nothing in thumbnails.py reads a user, role or permission, and grep_symbol found no "permission" anywhere. The only caller (gallery.py:14) renders the result into an image tag. The cache holds rendered images, not an authorization decision.
"""

HN_ATOMIC_UPDATE_PROMPT = """CODE CHUNK (inventory.py:decrement_stock_atomic, lines 1-8):
def decrement_stock_atomic(sku, count=1):
    updated = db.execute(
        "UPDATE stock SET quantity = quantity - %s WHERE sku = %s AND quantity >= %s",
        (count, sku, count),
    )
    if updated.rowcount == 0:
        raise OutOfStock()

CANDIDATE INCIDENTS:
OMEN-004: Check-then-act race condition without atomicity
  failure_mechanism: Code reads some state, decides an action is safe based on that read, then performs the action in a separate step with no lock or transaction spanning both - so two concurrent callers can both pass the check and both perform the action, violating an invariant that was assumed to hold.
  the_rule: Any check-then-act sequence on shared mutable state must be made atomic - a single `UPDATE ... WHERE` with the guard condition, a `SELECT ... FOR UPDATE`, or an equivalent compare-and-swap - not a read followed by a separate write.

INVESTIGATION TRANSCRIPT:
  [1] read_code({'file_path': 'inventory.py', 'start_line': 1, 'end_line': 20}) -> def decrement_stock_atomic(sku, count=1):
    updated = db.execute(
        "UPDATE stock SET quantity = quantity - %s WHERE sku = %s AND quantity >= %s",
        (count, sku, count),
    )
    if updated.rowcount == 0:
        raise OutOfStock()
  [2] get_incident({'incident_ref': 'OMEN-004'}) -> the_rule: ... a single `UPDATE ... WHERE` with the guard condition ...

INVESTIGATOR'S SUMMARY:
decrement_stock_atomic (inventory.py:1-7) performs the guard and the decrement in one UPDATE statement (`quantity >= %s` in the WHERE clause) and checks the affected row count afterwards. There is no separate read of the quantity, so two concurrent callers cannot both pass a stale check. This is the fix that OMEN-004's rule prescribes, not the failure.
"""


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("runner_cls", [runners.ADKRoleRunner, runners.DirectOllamaRunner], ids=["adk", "direct"])
@pytest.mark.parametrize(
    "prompt,ref",
    [(HN_THUMBNAIL_PROMPT, "OMEN-001"), (HN_ATOMIC_UPDATE_PROMPT, "OMEN-004")],
    ids=["thumbnail_cache", "atomic_update"],
)
async def test_live_adjudicator_does_not_confirm_a_hard_negative(runner_cls, prompt, ref):
    schema = adjudicator_schema([ref])
    verdict = await runner_cls().run_structured(prompts.load_prompt("adjudicator.txt"), prompt, schema, think=False)
    print(f"\n{runner_cls.__name__} {ref}: verdict={verdict.verdict} confidence={verdict.confidence} "
          f"evidence={verdict.evidence_lines} reasoning={verdict.reasoning}")
    assert verdict.verdict != "confirmed", f"hard negative confirmed: {verdict.reasoning}"
