"""Pure Python + Ollama /api/embed (via vectors.py): embed chunks
(hash-cached), query Chroma, collapse variant-level hits to incidents,
gate by similarity. No ADK, no LiteLLM, no generation — PLAN.md: routing
embeddings through the agent framework adds layers with nothing to gain.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from omen import store, vectors
from omen.config import (
    EMBED_BATCH_SIZE,
    EMBED_MODEL,
    QUERY_N_RESULTS,
    SIMILARITY_THRESHOLD,
    TOP_K_INCIDENTS,
)
from omen.contracts import Chunk, GatedChunk, RetrievalCandidate


@dataclass
class EmbedStats:
    n_total: int
    n_cache_hits: int
    n_cache_misses: int


def embed_chunks(conn: sqlite3.Connection, chunks: list[Chunk]) -> tuple[dict[str, list[float]], EmbedStats]:
    """content_hash -> embedding for every chunk, using the SQLite hash
    cache; Ollama is only called for cache misses, batched."""
    vectors_by_hash: dict[str, list[float]] = {}
    n_hits = 0
    misses_by_hash: dict[str, Chunk] = {}

    for chunk in chunks:
        cached = store.get_cached_vector(conn, chunk.content_hash, EMBED_MODEL)
        if cached is not None:
            vectors_by_hash[chunk.content_hash] = cached
            n_hits += 1
        else:
            misses_by_hash.setdefault(chunk.content_hash, chunk)

    unique_misses = list(misses_by_hash.values())
    for i in range(0, len(unique_misses), EMBED_BATCH_SIZE):
        batch = unique_misses[i : i + EMBED_BATCH_SIZE]
        embeddings = vectors.embed_document([c.content for c in batch])
        for chunk, embedding in zip(batch, embeddings):
            vectors_by_hash[chunk.content_hash] = embedding
            store.put_cached_vector(conn, chunk.content_hash, EMBED_MODEL, embedding)

    stats = EmbedStats(
        n_total=len(chunks),
        n_cache_hits=n_hits,
        n_cache_misses=len(chunks) - n_hits,
    )
    return vectors_by_hash, stats


def collapse_to_incidents(hits: list[dict]) -> list[RetrievalCandidate]:
    """Variant-level Chroma hits -> best-variant-per-incident, sorted
    descending, top `TOP_K_INCIDENTS`. Querying top-k directly at the
    variant level is the bug this guards against: three variants of one
    incident can fill every slot and starve the candidate list to one."""
    best: dict[str, RetrievalCandidate] = {}
    for hit in hits:
        ref = hit["metadata"]["incident_ref"]
        existing = best.get(ref)
        if existing is None or hit["similarity"] > existing.similarity:
            best[ref] = RetrievalCandidate(
                incident_ref=ref,
                similarity=hit["similarity"],
                matched_variant=hit["metadata"]["variant"],
            )
    ranked = sorted(best.values(), key=lambda c: c.similarity, reverse=True)
    return ranked[:TOP_K_INCIDENTS]


def gate(
    chunk: Chunk, candidates: list[RetrievalCandidate], threshold: float = SIMILARITY_THRESHOLD
) -> GatedChunk | None:
    """First of the two independent gates PLAN.md requires before a finding
    can surface (the second is the model's affirmative verdict, Phase 6+).
    Neither gate alone may flag."""
    passing = [c for c in candidates if c.similarity >= threshold]
    if not passing:
        return None
    return GatedChunk(chunk=chunk, candidates=passing)


def run(
    conn: sqlite3.Connection, chunks: list[Chunk], threshold: float = SIMILARITY_THRESHOLD
) -> tuple[list[GatedChunk], EmbedStats]:
    """Full Librarian pass: embed (cached), query, collapse, gate. Returns
    only chunks that cleared the similarity floor, plus embed-cache stats."""
    embeddings, stats = embed_chunks(conn, chunks)
    gated: list[GatedChunk] = []
    for chunk in chunks:
        hits = vectors.query_chunk(embeddings[chunk.content_hash], n_results=QUERY_N_RESULTS)
        candidates = collapse_to_incidents(hits)
        gated_chunk = gate(chunk, candidates, threshold=threshold)
        if gated_chunk:
            gated.append(gated_chunk)
    return gated, stats
