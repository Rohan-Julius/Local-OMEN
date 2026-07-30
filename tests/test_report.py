"""Phase 9 acceptance (PLAN.md): "Report: per-role progress with live
timings, terminal summary, persistence... Screen and DB timings come
from the same measurements, so they cannot disagree."

`report.py`'s own helpers (Stopwatch, ScanStats, print_summary) are pure
and tested directly, no model needed. The persistence wiring in
`cli.cmd_scan` is tested end-to-end against a fake RoleRunner — Triage/
Investigator/Adjudicator prompt *quality* is already covered live
elsewhere (test_triage.py, test_runners.py, test_adjudicator.py); this
file's job is only to prove the Scribe half — that `runs`, `findings`,
and `tool_calls` actually get populated from one scan. The embedding
step still hits live Ollama (cheap, no generation), so this skips
gracefully without it.
"""
import argparse
import time
import urllib.request

import pytest

from omen import cli, config, report, runners, store, tools as tools_mod, vectors
from omen.contracts import AdjudicatorVerdict, Finding, IncidentSeed, Transcript, TriageVerdict


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# report.py's pure helpers
# ---------------------------------------------------------------------------


def test_stopwatch_accumulates_across_laps():
    watch = report.Stopwatch()
    with watch.lap():
        time.sleep(0.01)
    with watch.lap():
        time.sleep(0.01)
    assert watch.total_ms >= 15  # two ~10ms laps, generous floor for CI jitter


def test_scan_stats_as_run_fields_matches_runs_columns():
    stats = report.ScanStats(n_files=2, n_chunks=5, n_gated=1, ms_embed=100)
    fields = stats.as_run_fields()
    expected_keys = {
        "n_files", "n_chunks", "n_cache_hits", "n_gated", "n_triaged", "n_investigated",
        "n_tool_calls", "n_confirmed", "ms_embed", "ms_triage", "ms_investigate", "ms_total",
    }
    assert set(fields) == expected_keys
    assert fields["n_chunks"] == 5
    assert fields["ms_embed"] == 100


def test_print_summary_renders_finding_fields(capsys):
    stats = report.ScanStats(n_chunks=3, n_gated=1, n_triaged=1, n_investigated=1, ms_total=1500)
    finding = Finding(
        run_id="r1",
        incident_ref="OMEN-001",
        file_path="permissions.py",
        start_line=3,
        end_line=5,
        symbol="has_access",
        similarity=0.9,
        verdict="confirmed",
        code_mechanism="caches a permission decision with no invalidation",
        reasoning="matches OMEN-001",
        evidence="permissions.py:3",
        confidence="high",
        tool_calls=2,
    )
    report.print_summary(stats, [finding])
    out = capsys.readouterr().out
    assert "OMEN-001" in out
    assert "permissions.py:has_access (3-5)" in out
    assert "caches a permission decision with no invalidation" in out
    assert "CONFIRMED" in out


def test_print_summary_handles_no_findings(capsys):
    report.print_summary(report.ScanStats(n_chunks=2), [])
    assert "no findings" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_scan persistence, end to end, against a fake RoleRunner
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.skipif(
    not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST (needed for the embed step)"
)


class _FakeRunner:
    """Stands in for ADKRoleRunner/DirectOllamaRunner so this test proves
    the Scribe's persistence plumbing, not prompt quality (covered live
    elsewhere) — deterministic and fast."""

    async def run_structured(self, instruction, prompt, schema, think=False):
        if schema is TriageVerdict:
            return TriageVerdict(
                code_mechanism="caches a permission decision with no invalidation",
                verdict="MATCH",
                confidence="high",
                reasoning="matches OMEN-001's stale-permission-cache mechanism",
            )
        if schema is AdjudicatorVerdict:
            return AdjudicatorVerdict(
                verdict="confirmed",
                code_mechanism="caches a permission decision with no invalidation",
                reasoning="matches OMEN-001",
                evidence_lines=["permissions.py:3: no cache_clear call found anywhere in the file"],
                confidence="high",
            )
        raise AssertionError(f"unexpected schema {schema}")

    async def run_tooled(self, instruction, prompt, tools, budget, think=True, on_step=None, on_text=None):
        wrapped, steps = tools_mod.budgeted_tools(tools, budget, on_step=on_step)
        by_name = {fn.__name__: fn for fn in wrapped}
        by_name["read_code"](file_path="permissions.py", start_line=1, end_line=5)
        return Transcript(steps=steps, final_text="checked for invalidation, found none", stopped_reason="model_finished")


def _scan_args(path: str) -> argparse.Namespace:
    return argparse.Namespace(path=path, since=None, all=False, dry_run=False, retrieval_only=False, runner="adk")


@pytest.mark.asyncio
async def test_cmd_scan_persists_run_findings_and_tool_calls(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "permissions.py").write_text(
        "import functools\n\n"
        "@functools.lru_cache(maxsize=1024)\n"
        "def has_access(user_id, resource_id):\n"
        "    return _compute_permission(user_id, resource_id)\n"
    )

    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: _FakeRunner())

    conn = real_connect(tmp_path / "test.db")
    store.upsert_incident(
        conn,
        IncidentSeed(
            ref="OMEN-001",
            title="Stale permission cache after revocation",
            failure_mechanism=(
                "An authorization decision is cached with no invalidation path tied to the "
                "permission-changing event."
            ),
            what_happened="irrelevant",
            the_rule="irrelevant",
            surface_forms=["functools.lru_cache wrapping a has_access or can_edit permission check"],
        ),
    )
    vectors.reindex(store.list_incidents(conn))

    await cli.cmd_scan(_scan_args(str(repo)))

    runs = conn.execute("SELECT * FROM runs").fetchall()
    assert len(runs) == 1, "expected exactly one run row"
    run = runs[0]
    assert run["mission"] == "scan"
    assert run["n_confirmed"] == 1
    assert run["finished_at"] is not None

    findings = conn.execute("SELECT * FROM findings").fetchall()
    assert len(findings) == 1
    assert findings[0]["incident_ref"] == "OMEN-001"
    assert findings[0]["verdict"] == "confirmed"
    assert findings[0]["run_id"] == run["run_id"]

    tool_calls = conn.execute("SELECT * FROM tool_calls").fetchall()
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "read_code"
    assert tool_calls[0]["run_id"] == run["run_id"]
    assert tool_calls[0]["ms"] is not None


@pytest.mark.asyncio
async def test_cmd_scan_retrieval_only_does_not_persist_a_run(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "permissions.py").write_text(
        "import functools\n\n"
        "@functools.lru_cache(maxsize=1024)\n"
        "def has_access(user_id, resource_id):\n"
        "    return _compute_permission(user_id, resource_id)\n"
    )

    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))

    conn = real_connect(tmp_path / "test.db")
    store.upsert_incident(
        conn,
        IncidentSeed(
            ref="OMEN-001",
            title="Stale permission cache after revocation",
            failure_mechanism="An authorization decision is cached with no invalidation path.",
            what_happened="irrelevant",
            the_rule="irrelevant",
            surface_forms=["functools.lru_cache wrapping a has_access or can_edit permission check"],
        ),
    )
    vectors.reindex(store.list_incidents(conn))

    args = _scan_args(str(repo))
    args.retrieval_only = True
    await cli.cmd_scan(args)

    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
