"""Phase 10 (PLAN.md "Harden and rehearse"): the checks that are cheap to
automate and expensive to discover live on the demo machine.

- Network isolation for the ADK/LiteLLM side (vectors.py's Chroma side is
  covered in test_vectors.py). Constructing an LlmAgent must not dial out —
  if it did, "fully local" would be a claim, not a proven property.
- Graceful degradation on an empty ledger, end to end through `omen scan`
  (PLAN.md Verification: "Graceful degradation... empty ledger... clear
  messages, no tracebacks").

Nothing here needs a live Ollama connection for the agent-construction
check; the empty-ledger scan still embeds real code chunks, so it skips
gracefully without Ollama, same as the rest of the live-model suite.
"""
from __future__ import annotations

import argparse
import socket
import urllib.request

import pytest

from omen import agents, cli, config, store, vectors
from omen.contracts import TriageVerdict


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


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


pytestmark = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))
    conn = real_connect(tmp_path / "test.db")
    # Deliberately no seed/reindex: this is the empty-ledger case.
    return conn


@pytest.mark.asyncio
async def test_scan_against_empty_ledger_gates_nothing_without_crashing(tmp_path, isolated_store):
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
    await cli.cmd_scan(args)  # must not raise

    runs = isolated_store.execute("SELECT * FROM runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["n_gated"] == 0
    assert runs[0]["n_confirmed"] == 0

    findings = isolated_store.execute("SELECT * FROM findings").fetchall()
    assert findings == []
