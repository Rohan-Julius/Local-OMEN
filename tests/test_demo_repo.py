"""Phase 9 acceptance (PLAN.md): "full scan renders progress, tool calls,
and a finding naming the incident ref, file, line span, and mechanism.
grep -ri redis demo_repo/ returns nothing and the scan still flags the
right chunk." Includes the negative control: the hard negative
(cache/response_cache.py) must never come back confirmed.

Live Ollama required (skips gracefully without it) — this drives the full
Scout -> Librarian -> Triage -> Investigator -> Adjudicator pipeline
against the real `demo_repo/`, against an isolated store seeded from the
real `incidents.yaml` (never the live on-disk ledger/Chroma index).
"""
import argparse
import subprocess
import urllib.request
from pathlib import Path

import pytest

from omen import cli, config, store, vectors

DEMO_REPO = Path(__file__).resolve().parent.parent / "demo_repo"
INCIDENTS_YAML = Path(__file__).resolve().parent.parent / "incidents.yaml"


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


def test_no_redis_anywhere_in_demo_repo():
    """PLAN.md: "grep -ri redis demo_repo/ returns nothing" — the true
    positive shares OMEN-001's mechanism with no shared vocabulary."""
    result = subprocess.run(
        ["grep", "-ril", "redis", str(DEMO_REPO)], capture_output=True, text=True
    )
    assert result.stdout.strip() == "", f"unexpected Redis mention(s): {result.stdout}"


pytestmark = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))
    conn = real_connect(tmp_path / "test.db")
    store.seed_from_yaml(conn, INCIDENTS_YAML)
    vectors.reindex(store.list_incidents(conn))
    return conn


def _scan_args() -> argparse.Namespace:
    # --all: demo_repo's working tree has no uncommitted diff (everything
    # is committed), so the default `git diff` scope would scan nothing.
    return argparse.Namespace(
        path=str(DEMO_REPO), since=None, all=True, dry_run=False, retrieval_only=False, runner="adk"
    )


@pytest.mark.asyncio
async def test_scan_confirms_permissions_and_rejects_response_cache(isolated_store):
    await cli.cmd_scan(_scan_args())

    runs = isolated_store.execute("SELECT * FROM runs").fetchall()
    assert len(runs) == 1
    # app.py, models.py, permissions.py, ratelimit.py, cache/response_cache.py, tests/test_ratelimit.py
    assert runs[0]["n_files"] == 6

    findings = isolated_store.execute("SELECT * FROM findings").fetchall()
    assert findings, "expected at least one finding to reach the Adjudicator"

    permissions_findings = [f for f in findings if f["file_path"] == "permissions.py"]
    assert permissions_findings, "expected Triage to flag something in permissions.py"
    confirmed = [f for f in permissions_findings if f["verdict"] == "confirmed"]
    assert confirmed, f"expected a confirmed finding in permissions.py, got verdicts {[f['verdict'] for f in permissions_findings]}"
    finding = confirmed[0]
    assert finding["incident_ref"] == "OMEN-001"
    assert finding["start_line"] is not None and finding["end_line"] is not None
    assert finding["code_mechanism"]
    assert finding["tool_calls"] and finding["tool_calls"] > 0

    # The negative control: a genuine cache, "cache" in every identifier,
    # mechanically unrelated (no authorization concept at all) — must
    # never be confirmed. If Triage rejected it outright there's no finding
    # row for it at all, which also satisfies this.
    response_cache_confirmed = [
        f for f in findings if "response_cache" in f["file_path"] and f["verdict"] == "confirmed"
    ]
    assert response_cache_confirmed == [], f"hard negative was confirmed: {response_cache_confirmed}"
