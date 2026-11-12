"""Phase 10 (PLAN.md "Harden and rehearse"): the checks that are cheap to
automate and expensive to discover live on the demo machine.

- Network isolation for the ADK/LiteLLM side (vectors.py's Chroma side is
  covered in test_vectors.py). Constructing an LlmAgent must not dial out —
  if it did, "fully local" would be a claim, not a proven property.
- Graceful degradation on an empty ledger, end to end through `omen scan`
  (PLAN.md Verification: "Graceful degradation... empty ledger... clear
  messages, no tracebacks").

Nothing here needs a live Ollama connection: agent construction makes no
call, and an empty-ledger scan now stops before embedding anything.
"""
from __future__ import annotations

import argparse
import socket

import pytest

from omen import agents, cli, store, vectors
from omen.contracts import TriageVerdict


def test_adk_agent_construction_touches_no_socket(monkeypatch):
    """PLAN.md: "ADK/LiteLLM credential probing" is one of the three
    network defaults this stack ships with. Building the agent object
    (LiteLlm model wrapper + LlmAgent) is where any eager credential or
    endpoint probing would happen — no run_async, no live call, so this
    needs no Ollama and runs every time, not just on the demo machine."""

    def _blocked_connect(self, address):
        raise AssertionError(f"unexpected socket connection attempt to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)

    agent = agents.LlmAgent(
        name="structured_role",
        model=agents._model(think=False),
        instruction="test instruction",
        output_schema=TriageVerdict,
        output_key="result",
    )
    assert agent.name == "structured_role"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))
    conn = real_connect(tmp_path / "test.db")
    # Deliberately no seed/reindex: this is the empty-ledger case.
    return conn


@pytest.mark.asyncio
async def test_scan_against_empty_ledger_stops_with_a_clear_message(tmp_path, isolated_store, monkeypatch, capsys):
    """Since Phase 3 (production plan, carry-over A): an empty ledger stops
    the scan with a clear message and exit code 1, before any embedding,
    instead of embedding the code and recording an empty run. No model
    or embedding call is made, so this no longer needs Ollama."""

    def no_embedding(prompts):
        raise AssertionError("scan embedded code against an empty ledger")

    monkeypatch.setattr(vectors, "_embed", no_embedding)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "import functools\n\n"
        "@functools.lru_cache(maxsize=1024)\n"
        "def has_access(user_id, resource_id):\n"
        "    return True\n"
    )

    args = argparse.Namespace(
        path=str(repo), since=None, all=True, dry_run=False, retrieval_only=False, runner="adk"
    )
    assert await cli.cmd_scan(args) == cli.EXIT_USER_ERROR  # must not raise

    out = capsys.readouterr().out
    assert "ledger is empty" in out
    assert "Traceback" not in out
    assert isolated_store.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert isolated_store.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
