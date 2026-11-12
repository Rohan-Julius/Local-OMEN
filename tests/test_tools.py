"""Phase 5 acceptance: unit tests per tool including a traversal attempt
and a duplicate-call hit. Every tool here is callable with no model
running — only the two search_memory/get_incident-touching live tests
need Ollama, and those skip gracefully without it.
"""
import subprocess
import time

import pytest

from omen import store, tools, vectors
from omen.tools import PathEscapesRoot, ToolBudget


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")


# ---------------------------------------------------------------------------
# read_code
# ---------------------------------------------------------------------------


def test_read_code_returns_exact_line_range(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\nline3\nline4\n")
    read_code = tools._make_read_code(tmp_path)
    assert read_code("a.py", 2, 3) == "line2\nline3"


def test_read_code_rejects_path_traversal(tmp_path):
    read_code = tools._make_read_code(tmp_path)
    with pytest.raises(PathEscapesRoot):
        read_code("../../../../etc/passwd", 1, 5)


def test_read_code_rejects_absolute_path_escape(tmp_path):
    read_code = tools._make_read_code(tmp_path)
    with pytest.raises(PathEscapesRoot):
        read_code("/etc/passwd", 1, 5)


def test_read_code_clamps_out_of_range_lines(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\n")
    read_code = tools._make_read_code(tmp_path)
    assert read_code("a.py", 1, 100) == "line1\nline2"


# ---------------------------------------------------------------------------
# grep_symbol
# ---------------------------------------------------------------------------


def test_grep_symbol_finds_definition_and_call_site(tmp_path):
    (tmp_path / "a.py").write_text("def has_access(u):\n    pass\n")
    (tmp_path / "b.py").write_text("if has_access(user):\n    pass\n")
    grep_symbol = tools._make_grep_symbol(tmp_path)
    hits = grep_symbol("has_access")
    assert any("a.py:1:" in h for h in hits)
    assert any("b.py:1:" in h for h in hits)


def test_grep_symbol_matches_whole_word_only(tmp_path):
    (tmp_path / "a.py").write_text("def has_access_v2(u):\n    pass\n")
    grep_symbol = tools._make_grep_symbol(tmp_path)
    assert grep_symbol("has_access") == []


def test_grep_symbol_skips_venv_directories(tmp_path):
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "a.py").write_text("def has_access(u):\n    pass\n")
    grep_symbol = tools._make_grep_symbol(tmp_path)
    assert grep_symbol("has_access") == []


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def test_read_file_returns_contents(tmp_path):
    (tmp_path / "postmortem.md").write_text("# Incident\nsomething broke\n")
    read_file = tools._make_read_file(tmp_path)
    assert read_file("postmortem.md") == "# Incident\nsomething broke\n"


def test_read_file_rejects_path_traversal(tmp_path):
    read_file = tools._make_read_file(tmp_path)
    with pytest.raises(PathEscapesRoot):
        read_file("../../../../etc/passwd")


# ---------------------------------------------------------------------------
# read_commit / read_diff
# ---------------------------------------------------------------------------


def test_read_commit_and_read_diff(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-qm", "initial commit message")
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    read_commit = tools._make_read_commit(tmp_path)
    commit_info = read_commit(sha)
    assert sha in commit_info
    assert "initial commit message" in commit_info

    read_diff = tools._make_read_diff(tmp_path)
    diff = read_diff(sha, "a.py")
    assert "+def f():" in diff


def test_read_diff_truncates_at_line_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "DIFF_LINE_CAP", 5)
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(50)) + "\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-qm", "big file")
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    read_diff = tools._make_read_diff(tmp_path)
    diff = read_diff(sha, "a.py")
    assert "truncated" in diff
    assert len(diff.splitlines()) <= 6  # 5 lines + the truncation notice


def test_read_commit_reports_error_for_unknown_sha(tmp_path):
    _init_repo(tmp_path)
    read_commit = tools._make_read_commit(tmp_path)
    assert "error" in read_commit("0000000000000000000000000000000000000").lower()


# ---------------------------------------------------------------------------
# write_incident (isolated store, no network)
# ---------------------------------------------------------------------------


def test_write_incident_creates_and_auto_increments_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "embed_query", lambda texts: [[1.0] for _ in texts])

    conn = store.connect(tmp_path / "test.db")
    write_incident = tools._make_write_incident(conn, learned_by="archivist:git", source="git:abc123")

    ref1 = write_incident(
        title="First",
        failure_mechanism="mechanism one",
        what_happened="happened one",
        the_rule="rule one",
        surface_forms=["form a"],
    )
    ref2 = write_incident(
        title="Second",
        failure_mechanism="mechanism two",
        what_happened="happened two",
        the_rule="rule two",
        surface_forms=["form b"],
    )
    assert ref1 != ref2

    incident1 = store.get_incident(conn, ref1)
    assert incident1.learned_by == "archivist:git"
    assert incident1.source == "git:abc123"


def test_write_incident_updates_when_given_existing_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "embed_query", lambda texts: [[1.0] for _ in texts])

    conn = store.connect(tmp_path / "test.db")
    write_incident = tools._make_write_incident(conn, learned_by="archivist:postmortem", source="postmortem:x")

    ref = write_incident(
        title="Original title",
        failure_mechanism="m",
        what_happened="h",
        the_rule="r",
        surface_forms=["form"],
    )
    same_ref = write_incident(
        title="Updated title",
        failure_mechanism="m2",
        what_happened="h2",
        the_rule="r2",
        surface_forms=["form2"],
        existing_ref=ref,
    )
    assert same_ref == ref
    incident = store.get_incident(conn, ref)
    assert incident.title == "Updated title"
    assert len(store.list_incidents(conn)) == 1  # updated in place, not duplicated


# ---------------------------------------------------------------------------
# search_memory / get_incident: live Ollama, isolated Chroma path
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_search_memory_and_get_incident(tmp_path, monkeypatch):
    from omen.contracts import Incident

    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    conn = store.connect(tmp_path / "test.db")

    incident = Incident(
        id=1,
        ref="OMEN-999",
        title="Test incident",
        failure_mechanism="a cached permission check with no invalidation on revoke",
        what_happened="irrelevant",
        the_rule="irrelevant",
        learned_by="seed",
        created_at="2026-01-01",
        surface_forms=["a functools.lru_cache wrapping a permission check"],
    )
    store.upsert_incident(conn, incident)
    vectors.reindex([incident])

    search_memory = tools._make_search_memory(conn)
    results = search_memory("permission cache that never invalidates on revoke")
    assert any("OMEN-999" in r for r in results)

    get_incident = tools._make_get_incident(conn)
    full = get_incident("OMEN-999")
    assert "Test incident" in full
    assert "irrelevant" in full

    assert "no incident found" in get_incident("OMEN-000").lower()


# ---------------------------------------------------------------------------
# ToolBudget: the three hard caps
# ---------------------------------------------------------------------------


def test_budget_duplicate_call_served_from_cache_without_reinvoking():
    calls = []

    def fake_tool(x):
        calls.append(x)
        return f"result:{x}"

    budget = ToolBudget(max_calls=6, wall_clock_seconds=90)
    tools_dict = {"fake_tool": fake_tool}

    ok1, result1, note1 = budget.invoke(tools_dict, "fake_tool", {"x": "a"})
    ok2, result2, note2 = budget.invoke(tools_dict, "fake_tool", {"x": "a"})

    assert ok1 and ok2
    assert result1 == result2 == "result:a"
    assert note1 is None
    assert "repeat" in note2
    assert calls == ["a"]  # the underlying tool ran exactly once


def test_budget_duplicate_detection_is_kwarg_order_independent():
    def fake_tool(a, b):
        return f"{a}-{b}"

    budget = ToolBudget(max_calls=6, wall_clock_seconds=90)
    tools_dict = {"fake_tool": fake_tool}
    budget.invoke(tools_dict, "fake_tool", {"a": 1, "b": 2})
    _, _, note = budget.invoke(tools_dict, "fake_tool", {"b": 2, "a": 1})
    assert note is not None and "repeat" in note


def test_budget_trips_call_cap():
    def fake_tool(x):
        return x

    budget = ToolBudget(max_calls=2, wall_clock_seconds=90)
    tools_dict = {"fake_tool": fake_tool}
    budget.invoke(tools_dict, "fake_tool", {"x": 1})
    budget.invoke(tools_dict, "fake_tool", {"x": 2})
    ok, result, note = budget.invoke(tools_dict, "fake_tool", {"x": 3})
    assert ok is False
    assert result is None
    assert "cap" in note or "exceeded" in note


def test_budget_trips_wall_clock_ceiling():
    def fake_tool():
        return "ok"

    budget = ToolBudget(max_calls=6, wall_clock_seconds=0.05)
    assert budget.invoke({"fake_tool": fake_tool}, "fake_tool", {})[0] is True  # starts the clock (B5)
    time.sleep(0.1)
    ok, result, note = budget.invoke({"fake_tool": fake_tool}, "fake_tool", {})
    assert ok is False
    assert "wall-clock" in note


def test_budget_catches_tool_exceptions_and_caches_the_error():
    def raises(x):
        raise ValueError("boom")

    budget = ToolBudget(max_calls=6, wall_clock_seconds=90)
    ok, result, note = budget.invoke({"raises": raises}, "raises", {"x": 1})
    assert ok is True  # the call itself completed; the *tool* failed, not the budget
    assert "boom" in result
    assert note == "error"  # B10: marked, so it is never mistaken for a real result

    ok, cached, note = budget.invoke({"raises": raises}, "raises", {"x": 1})
    assert cached == result
    assert "repeat" in note


def test_budget_converts_path_escape_to_error_string(tmp_path):
    read_code = tools._make_read_code(tmp_path)
    budget = ToolBudget(max_calls=6, wall_clock_seconds=90)
    ok, result, note = budget.invoke(
        {"read_code": read_code}, "read_code", {"file_path": "../../etc/passwd", "start_line": 1, "end_line": 5}
    )
    assert ok is True
    assert "escapes" in result


def test_budget_rejects_unknown_tool_name():
    budget = ToolBudget(max_calls=6, wall_clock_seconds=90)
    ok, result, note = budget.invoke({}, "not_a_real_tool", {})
    assert ok is False
    assert "unknown tool" in note


# ---------------------------------------------------------------------------
# Tool set composition — PLAN.md: scoped per mission, never pooled
# ---------------------------------------------------------------------------


def test_scan_tools_has_exactly_four_tools(tmp_path):
    conn = store.connect(tmp_path / "test.db")
    scan_tools = tools.build_scan_tools(tmp_path, conn)
    assert set(scan_tools) == {"read_code", "grep_symbol", "search_memory", "get_incident"}


def test_learn_postmortem_tools_has_exactly_three_tools(tmp_path):
    conn = store.connect(tmp_path / "test.db")
    learn_tools = tools.build_learn_postmortem_tools(conn, tmp_path, source="postmortem:x")
    assert set(learn_tools) == {"read_file", "search_memory", "write_incident"}


def test_learn_git_tools_has_exactly_four_tools(tmp_path):
    conn = store.connect(tmp_path / "test.db")
    learn_tools = tools.build_learn_git_tools(conn, tmp_path, source="git:x")
    assert set(learn_tools) == {"read_commit", "read_diff", "search_memory", "write_incident"}


# ---------------------------------------------------------------------------
# Phase 1 regressions (docs/PRODUCTION_PLAN.md; bug IDs from the audit's
# Appendix B). Each test reproduced its bug before the fix.
# ---------------------------------------------------------------------------


def _commit_one_file(repo) -> str:
    _init_repo(repo)
    (repo / "a.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "initial")
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_b3_read_commit_rejects_option_injection_and_writes_no_file(tmp_path):
    """B3: a model-chosen `sha` of `--output=<path>` used to reach
    `git show` as an option and write an arbitrary file on the host."""
    repo = tmp_path / "repo"
    _commit_one_file(repo)
    target = tmp_path / "written_by_model.txt"

    result = tools._make_read_commit(repo)(f"--output={target}")

    assert result.startswith("error")
    assert not target.exists()


def test_b3_read_diff_rejects_option_injection_and_writes_no_file(tmp_path):
    repo = tmp_path / "repo"
    _commit_one_file(repo)
    target = tmp_path / "written_by_model.txt"

    result = tools._make_read_diff(repo)(f"--output={target}", "a.py")

    assert result.startswith("error")
    assert not target.exists()


def test_b3_read_commit_still_accepts_full_and_abbreviated_shas(tmp_path):
    repo = tmp_path / "repo"
    sha = _commit_one_file(repo)
    read_commit = tools._make_read_commit(repo)
    assert sha in read_commit(sha)
    assert sha in read_commit(sha[:8])
    assert "+def f():" in tools._make_read_diff(repo)(sha[:8], "a.py")


def test_b4_read_file_refuses_secrets_store_and_git_inside_its_root(tmp_path):
    """B4: the Archivist's read_file must never return `.env`, a SQLite
    database, anything under `omen_store/`, or anything under `.git/`,
    even when those sit inside the allowed root."""
    (tmp_path / ".env").write_text("FAKE_SECRET=do-not-leak\n")
    (tmp_path / ".env.local").write_text("FAKE_SECRET=do-not-leak\n")
    (tmp_path / "omen_store").mkdir()
    (tmp_path / "omen_store" / "notes.txt").write_text("FAKE_SECRET=do-not-leak\n")
    (tmp_path / "omen_store" / "omen.db").write_text("FAKE_SECRET=do-not-leak\n")
    (tmp_path / "other.db").write_text("FAKE_SECRET=do-not-leak\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("FAKE_SECRET=do-not-leak\n")
    (tmp_path / "postmortem.md").write_text("# fine to read\n")

    read_file = tools._make_read_file(tmp_path)
    budget = ToolBudget(max_calls=20, wall_clock_seconds=90)
    for path in [".env", ".env.local", "omen_store/notes.txt", "omen_store/omen.db", "other.db", ".git/config"]:
        ok, result, _ = budget.invoke({"read_file": read_file}, "read_file", {"file_path": path})
        assert ok
        assert "FAKE_SECRET" not in result, path
        assert result.startswith("error"), path

    assert read_file("postmortem.md") == "# fine to read\n"


def test_b5_wall_clock_starts_at_the_first_tool_call():
    """B5: a slow first model turn (before any tool call) used to use up
    the whole wall-clock budget, so the first tool call was refused."""
    budget = ToolBudget(max_calls=6, wall_clock_seconds=0.05)
    time.sleep(0.1)  # stands in for a slow first LLM turn on CPU
    ok, result, note = budget.invoke({"fake_tool": lambda: "ok"}, "fake_tool", {})
    assert ok is True
    assert result == "ok"
    assert note is None


def _isolated_write_incident(tmp_path, monkeypatch, learned_by="archivist:postmortem"):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "embed_query", lambda texts: [[1.0] for _ in texts])
    conn = store.connect(tmp_path / "test.db")
    return conn, tools._make_write_incident(conn, learned_by=learned_by, source="test")


_INCIDENT_FIELDS = dict(
    title="t", failure_mechanism="m", what_happened="h", the_rule="r", surface_forms=["f"]
)


@pytest.mark.parametrize("bad_ref", ["not-a-ref", "OMEN-7", "omen-001", "OMEN-001; DROP TABLE incidents", "OMEN-042"])
def test_b9_write_incident_rejects_malformed_or_unknown_existing_ref(tmp_path, monkeypatch, bad_ref):
    """B9: any `existing_ref` string used to become a new ref. It must
    match the ref format AND name an incident that exists."""
    conn, write_incident = _isolated_write_incident(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        write_incident(**_INCIDENT_FIELDS, existing_ref=bad_ref)
    assert store.list_incidents(conn) == []


def test_b10_tool_exception_is_marked_with_an_error_note():
    def raises(x):
        raise ValueError("boom")

    budget = ToolBudget(max_calls=6, wall_clock_seconds=90)
    ok, result, note = budget.invoke({"raises": raises}, "raises", {"x": 1})
    assert ok is True
    assert "boom" in result
    assert note == "error"


def test_b10_written_ref_ignores_a_failed_write_incident(tmp_path, monkeypatch):
    """B10: a write_incident that raised used to be recorded with
    note=None, so `written_ref` reported "error: ..." as the new ref."""
    import sqlite3

    from omen import archivist
    from omen.contracts import Transcript

    conn, write_incident = _isolated_write_incident(tmp_path, monkeypatch)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "upsert_incident", locked)
    wrapped, steps = tools.budgeted_tools({"write_incident": write_incident}, ToolBudget())
    wrapped[0](**_INCIDENT_FIELDS)

    assert steps[0].result.startswith("error")
    assert archivist.written_ref(Transcript(steps=steps, final_text="", stopped_reason="model_finished")) is None


def test_b11_ref_is_not_reused_after_forgetting_the_newest(tmp_path, monkeypatch):
    conn, write_incident = _isolated_write_incident(tmp_path, monkeypatch)
    first = write_incident(**_INCIDENT_FIELDS)
    second = write_incident(**_INCIDENT_FIELDS)
    assert (first, second) == ("OMEN-001", "OMEN-002")

    assert store.forget_incident(conn, second)
    assert write_incident(**_INCIDENT_FIELDS) == "OMEN-003"


def test_b11_ref_is_not_reused_after_forgetting_a_seeded_incident(tmp_path, monkeypatch):
    """Seeded refs are set explicitly (incidents.yaml), not allocated by
    _next_ref, so forgetting the newest seeded incident must also retire
    its number."""
    from omen.contracts import IncidentSeed

    conn, write_incident = _isolated_write_incident(tmp_path, monkeypatch)
    for n in (1, 2, 3):
        store.upsert_incident(conn, IncidentSeed(ref=f"OMEN-00{n}", **_INCIDENT_FIELDS))
    assert store.forget_incident(conn, "OMEN-003")
    assert write_incident(**_INCIDENT_FIELDS) == "OMEN-004"
