"""Phase 3 (production plan), audit B6: evidence validation for a
`confirmed` Adjudicator verdict. Pure: no model, no network, files in a
temporary repo only.
"""
from __future__ import annotations

import json

import pytest

from omen import evidence
from omen.contracts import AdjudicatorVerdict, Chunk, ToolCallStep, Transcript

PERMISSIONS = "\n".join(
    [
        "import functools",  # 1
        "",  # 2
        "@functools.lru_cache(maxsize=1024)",  # 3  chunk start
        "def has_access(user_id, resource_id):",  # 4
        "    return _compute_permission(user_id, resource_id)",  # 5  chunk end
        "",  # 6
        "",  # 7
        "def revoke(user_id, resource_id):",  # 8
        "    _store.delete(user_id, resource_id)",  # 9
        "    # no has_access.cache_clear() here",  # 10
    ]
) + "\n"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "permissions.py").write_text(PERMISSIONS, encoding="utf-8")
    (tmp_path / "other.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    return tmp_path


CHUNK = Chunk(
    file_path="permissions.py", start_line=3, end_line=5, symbol="has_access", content="...", content_hash="h"
)


def confirmed(*lines: str) -> AdjudicatorVerdict:
    return AdjudicatorVerdict(
        verdict="confirmed", code_mechanism="m", reasoning="r", evidence_lines=list(lines), confidence="high"
    )


def read(file_path: str, start: int, end: int, result: str = "...code...", note: str | None = None) -> ToolCallStep:
    return ToolCallStep(
        tool_name="read_code", args={"file_path": file_path, "start_line": start, "end_line": end}, result=result, note=note
    )


def grep(symbol: str, hits: list[str], note: str | None = None) -> ToolCallStep:
    return ToolCallStep(tool_name="grep_symbol", args={"symbol": symbol}, result=json.dumps(hits), note=note)


def transcript(*steps: ToolCallStep) -> Transcript:
    return Transcript(steps=list(steps), final_text="summary", stopped_reason="model_finished")


def check(repo, verdict, t, **kwargs):
    return evidence.validate_confirmed(verdict, CHUNK, t, repo, **kwargs)


# ---------------------------------------------------------------------------
# Downgrades
# ---------------------------------------------------------------------------


def test_no_evidence_lines_is_downgraded(repo):
    final, reason = check(repo, confirmed(), transcript(read("permissions.py", 1, 10)))
    assert final.verdict == "unverified"
    assert "no evidence_lines" in reason


@pytest.mark.parametrize("line", ["permissions.py", "line 4 of permissions.py", "permissions.py:abc", ":4", "C:\\x\\permissions.py:4"])
def test_malformed_citation_is_downgraded(repo, line):
    final, reason = check(repo, confirmed(line), transcript(read("permissions.py", 1, 10)))
    assert final.verdict == "unverified"
    assert reason


def test_nonexistent_file_is_downgraded(repo):
    final, reason = check(repo, confirmed("missing.py:1"), transcript(read("permissions.py", 1, 10)))
    assert final.verdict == "unverified"
    assert "does not exist" in reason


def test_file_outside_the_repo_root_is_downgraded(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.py"
    outside.write_text("x = 1\n")
    final, reason = check(repo, confirmed(f"../{outside.parent.name}/secret.py:1"), transcript(read("permissions.py", 1, 10)))
    assert final.verdict == "unverified"


@pytest.mark.parametrize("line", ["permissions.py:11", "permissions.py:9-12", "permissions.py:0", "permissions.py:5-4"])
def test_line_out_of_range_is_downgraded(repo, line):
    final, reason = check(repo, confirmed(line), transcript(read("permissions.py", 1, 10)))
    assert final.verdict == "unverified"
    assert reason


def test_cpu_case_chunk_citation_with_zero_successful_tool_calls_is_not_confirmed(repo):
    """The audit's CPU scenario: every tool call was refused by the wall
    clock, and the Adjudicator still confirmed, citing the chunk's own
    lines. Default: chunk-only citations cannot confirm when the
    Investigator gathered nothing."""
    refused = read("permissions.py", 1, 10, result="", note="wall-clock ceiling exceeded - stop calling tools and answer now")
    for t in (transcript(), transcript(refused)):
        final, reason = check(repo, confirmed("permissions.py:3", "permissions.py:4-5"), t)
        assert final.verdict == "unverified"
        assert "no successful tool call" in reason


def test_chunk_only_rule_can_be_turned_off_explicitly(repo):
    final, reason = check(repo, confirmed("permissions.py:3"), transcript(), allow_chunk_only=True)
    assert (final.verdict, reason) == ("confirmed", None)


def test_citation_outside_chunk_and_reads_is_downgraded(repo):
    final, reason = check(repo, confirmed("permissions.py:9"), transcript(read("permissions.py", 1, 5)))
    assert final.verdict == "unverified"
    assert "outside the chunk" in reason


def test_one_bad_line_among_good_ones_downgrades(repo):
    final, reason = check(repo, confirmed("permissions.py:4", "other.py:2"), transcript(read("permissions.py", 1, 10)))
    assert final.verdict == "unverified"
    assert "other.py:2" in reason


@pytest.mark.parametrize(
    "step",
    [
        read("permissions.py", 8, 10, result="", note="max 6 tool calls exceeded - stop calling tools and answer now"),
        read("permissions.py", 8, 10, result="error: ValueError: boom", note="error"),
        read("permissions.py", 8, 10, result="error reading permissions.py: [Errno 13]"),  # no note, but an error result
        read("permissions.py", 8, 10, note="repeat call - same result as before, already retrieved above"),
        grep("cache_clear", ["permissions.py:10: # no has_access.cache_clear() here"], note="error"),
    ],
    ids=["call-cap", "raised", "error-result", "duplicate", "grep-error"],
)
def test_refused_or_errored_calls_do_not_count_as_read(repo, step):
    """A successful get_incident call keeps the zero-tool-call rule out of
    the way, so this checks only whether `step` counts as a read."""
    ok = ToolCallStep(tool_name="get_incident", args={"incident_ref": "OMEN-001"}, result="OMEN-001: ...")
    final, reason = check(repo, confirmed("permissions.py:10"), transcript(ok, step))
    assert final.verdict == "unverified"
    assert "outside the chunk" in reason


# ---------------------------------------------------------------------------
# Kept as confirmed
# ---------------------------------------------------------------------------


def test_transcript_backed_citation_is_confirmed(repo):
    t = transcript(read("permissions.py", 8, 10))
    final, reason = check(repo, confirmed("permissions.py:10: no cache_clear call", "permissions.py:9-10"), t)
    assert (final.verdict, reason) == ("confirmed", None)


def test_read_code_range_is_clamped_like_the_tool(repo):
    t = transcript(read("permissions.py", 8, 500))  # read_code clamps end to the file's 10 lines
    final, reason = check(repo, confirmed("permissions.py:10"), t)
    assert (final.verdict, reason) == ("confirmed", None)


def test_grep_hit_location_counts_as_read(repo):
    t = transcript(grep("cache_clear", ["permissions.py:10: # no has_access.cache_clear() here"]))
    final, reason = check(repo, confirmed("`permissions.py:10`"), t)
    assert (final.verdict, reason) == ("confirmed", None)


def test_chunk_citation_is_confirmed_once_the_investigator_gathered_something(repo):
    t = transcript(grep("cache_clear", []))  # searched, found no invalidation anywhere
    final, reason = check(repo, confirmed("permissions.py:3-5"), t)
    assert (final.verdict, reason) == ("confirmed", None)


@pytest.mark.parametrize("verdict", ["rejected", "unverified"])
def test_non_confirmed_verdicts_pass_through_unchanged(repo, verdict):
    v = AdjudicatorVerdict(verdict=verdict, code_mechanism="m", reasoning="r", evidence_lines=["nope"], confidence="low")
    assert check(repo, v, transcript()) == (v, None)
