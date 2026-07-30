"""Phase 3 acceptance: the known-positive chunk retrieves its incident in
the top 3, and a second run hits the embed cache 100%. Pure-logic pieces
(collapse, gate) are tested without any network dependency; the full
pipeline test needs a live Ollama and is skipped gracefully if one isn't
reachable.
"""
import urllib.request

import pytest

from omen import librarian, store, vectors
from omen.config import OLLAMA_HOST
from omen.contracts import Chunk, Incident


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


def _hit(ref: str, similarity: float, variant: str) -> dict:
    return {
        "id": f"{ref}::{variant}",
        "similarity": similarity,
        "metadata": {"incident_ref": ref, "variant": variant},
        "document": "irrelevant for these tests",
    }


# ---------------------------------------------------------------------------
# Pure logic: no network, no store
# ---------------------------------------------------------------------------


def test_collapse_takes_best_variant_per_incident():
    hits = [
        _hit("OMEN-001", 0.40, "mechanism"),
        _hit("OMEN-001", 0.55, "surface:0"),  # better variant, same incident
        _hit("OMEN-001", 0.20, "surface:1"),
    ]
    candidates = librarian.collapse_to_incidents(hits)
    assert len(candidates) == 1
    assert candidates[0].incident_ref == "OMEN-001"
    assert candidates[0].similarity == 0.55
    assert candidates[0].matched_variant == "surface:0"


def test_collapse_does_not_let_one_incident_starve_the_top_k():
    """The bug PLAN.md calls out: querying top-3 at the variant level can
    fill all 3 slots with one incident's variants and hide everything else."""
    hits = [
        _hit("OMEN-001", 0.90, "mechanism"),
        _hit("OMEN-001", 0.85, "surface:0"),
        _hit("OMEN-001", 0.80, "surface:1"),
        _hit("OMEN-002", 0.50, "mechanism"),
    ]
    candidates = librarian.collapse_to_incidents(hits)
    refs = [c.incident_ref for c in candidates]
    assert "OMEN-002" in refs


def test_collapse_caps_at_top_k_incidents():
    hits = [_hit(f"OMEN-{i:03}", 1.0 - i * 0.1, "mechanism") for i in range(5)]
    candidates = librarian.collapse_to_incidents(hits)
    assert len(candidates) == librarian.TOP_K_INCIDENTS


def test_collapse_sorts_descending_by_similarity():
    hits = [_hit("OMEN-001", 0.2, "mechanism"), _hit("OMEN-002", 0.9, "mechanism")]
    candidates = librarian.collapse_to_incidents(hits)
    assert [c.incident_ref for c in candidates] == ["OMEN-002", "OMEN-001"]


def test_gate_rejects_when_nothing_clears_threshold():
    candidates = librarian.collapse_to_incidents([_hit("OMEN-001", 0.1, "mechanism")])
    chunk = Chunk(file_path="a.py", start_line=1, end_line=2, symbol="f", content="x", content_hash="h")
    assert librarian.gate(chunk, candidates, threshold=0.35) is None


def test_gate_keeps_only_candidates_above_threshold():
    candidates = librarian.collapse_to_incidents(
        [_hit("OMEN-001", 0.50, "mechanism"), _hit("OMEN-002", 0.10, "mechanism")]
    )
    chunk = Chunk(file_path="a.py", start_line=1, end_line=2, symbol="f", content="x", content_hash="h")
    gated = librarian.gate(chunk, candidates, threshold=0.35)
    assert gated is not None
    assert [c.incident_ref for c in gated.candidates] == ["OMEN-001"]


# ---------------------------------------------------------------------------
# Embed cache: monkeypatched embedder, no network, real (temp) SQLite
# ---------------------------------------------------------------------------


def test_embed_chunks_hits_cache_on_second_call(tmp_path, monkeypatch):
    calls = []

    def fake_embed_document(texts, title="none"):
        calls.append(list(texts))
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr(vectors, "embed_document", fake_embed_document)

    conn = store.connect(tmp_path / "test.db")
    chunks = [
        Chunk(file_path="a.py", start_line=1, end_line=2, symbol="f", content="aaa", content_hash="h1"),
        Chunk(file_path="b.py", start_line=1, end_line=2, symbol="g", content="bbbb", content_hash="h2"),
    ]

    _, stats1 = librarian.embed_chunks(conn, chunks)
    assert stats1.n_cache_hits == 0
    assert stats1.n_cache_misses == 2
    assert len(calls) == 1  # one batch call for both misses

    calls.clear()
    _, stats2 = librarian.embed_chunks(conn, chunks)
    assert stats2.n_cache_hits == 2
    assert stats2.n_cache_misses == 0
    assert calls == []  # no embedder call at all on full cache hit


def test_embed_chunks_dedups_identical_content_within_one_call(tmp_path, monkeypatch):
    calls = []

    def fake_embed_document(texts, title="none"):
        calls.append(list(texts))
        return [[1.0] for _ in texts]

    monkeypatch.setattr(vectors, "embed_document", fake_embed_document)

    conn = store.connect(tmp_path / "test.db")
    same_hash_chunks = [
        Chunk(file_path="a.py", start_line=1, end_line=2, symbol="f", content="dup", content_hash="hdup"),
        Chunk(file_path="b.py", start_line=1, end_line=2, symbol="g", content="dup", content_hash="hdup"),
    ]
    librarian.embed_chunks(conn, same_hash_chunks)
    assert len(calls[0]) == 1  # embedded once despite two chunks sharing a content hash


# ---------------------------------------------------------------------------
# Full pipeline, live Ollama, isolated Chroma path (skips if unreachable)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")
def test_known_positive_chunk_retrieves_its_incident_in_top_3(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")

    incident = Incident(
        id=1,
        ref="OMEN-001",
        title="Stale permission cache after revocation",
        failure_mechanism=(
            "An authorization decision is cached with no invalidation path tied to "
            "the permission-changing event, so a revoked permission keeps succeeding."
        ),
        what_happened="irrelevant here",
        the_rule="irrelevant here",
        learned_by="seed",
        created_at="2026-01-01",
        surface_forms=["functools.lru_cache wrapping a has_access or can_edit permission check"],
    )
    n = vectors.reindex([incident])
    assert n == 2  # 1 mechanism + 1 surface form

    conn = store.connect(tmp_path / "test.db")
    chunk = Chunk(
        file_path="permissions.py",
        start_line=1,
        end_line=5,
        symbol="has_access",
        content=(
            "@functools.lru_cache(maxsize=1024)\n"
            "def has_access(user_id, resource_id):\n"
            "    return _compute_permission(user_id, resource_id)"
        ),
        content_hash="known-positive",
    )

    gated, stats = librarian.run(conn, [chunk])
    assert stats.n_cache_misses == 1
    assert len(gated) == 1
    refs = [c.incident_ref for c in gated[0].candidates]
    assert "OMEN-001" in refs
