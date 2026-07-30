"""Phase 10 hardening (PLAN.md): "Three libraries in this stack default to
talking to the internet... this is the only thing that proves they don't."
Chroma is two of the three (telemetry, the auto-downloading default
embedding function) — this file pins both, plus the empty-ledger query
shape (PLAN.md Verification: "Graceful degradation... empty ledger").

No live Ollama needed anywhere in this file: `reindex` is exercised with
`embed_query` monkeypatched to a fixed vector, so these tests prove
Chroma's own behavior in isolation, not embedding quality (covered live
elsewhere).
"""
from __future__ import annotations

import os
import socket

import pytest

from omen import vectors
from omen.contracts import Incident


@pytest.fixture(autouse=True)
def isolated_chroma(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")


def _fake_incident(ref: str = "OMEN-999") -> Incident:
    return Incident(
        id=1,
        ref=ref,
        title="test incident",
        failure_mechanism="a thing fails a certain way",
        what_happened="irrelevant",
        the_rule="irrelevant",
        surface_forms=["a concrete manifestation"],
        created_at="2026-01-01T00:00:00",
    )


def test_anonymized_telemetry_env_var_is_set():
    """Belt and braces alongside Settings(anonymized_telemetry=False) —
    importing omen.vectors alone must be enough, before any client is
    constructed."""
    assert os.environ.get("ANONYMIZED_TELEMETRY") == "FALSE"


def test_query_chunk_against_empty_collection_returns_no_hits():
    """An empty ledger (no incidents ever seeded/learned) must not crash
    the Librarian — `omen scan` should just gate nothing, not raise."""
    vectors.reindex([])  # no incidents: creates an empty collection
    hits = vectors.query_chunk([0.1] * 768, n_results=6)
    assert hits == []


def test_reindex_and_query_touch_no_socket(monkeypatch):
    """The concrete proof, not an assertion of intent: Chroma's own client
    construction, collection (re)build, and query must be pure local disk
    I/O with telemetry off. Any TCP connect attempt during this block —
    to any address — fails the test immediately, so a Settings regression
    (or a Chroma upgrade that reintroduces network defaults) is caught
    here rather than discovered live on the demo machine."""

    def _blocked_connect(self, address):
        raise AssertionError(f"unexpected socket connection attempt to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(vectors, "embed_query", lambda texts: [[0.1] * 768 for _ in texts])

    n = vectors.reindex([_fake_incident()])
    assert n == 2  # one mechanism variant + one surface form

    hits = vectors.query_chunk([0.1] * 768, n_results=6)
    assert len(hits) == 2
    assert {h["metadata"]["incident_ref"] for h in hits} == {"OMEN-999"}

    vectors.delete_incident_variants("OMEN-999")
    assert vectors.count() == 0
