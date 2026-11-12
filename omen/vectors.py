"""The only module that imports chromadb. Owns the `incidents` collection,
the Ollama embedding calls that feed it (never routed through ADK/LiteLLM —
see PLAN.md), and the distance -> similarity conversion boundary: no raw
Chroma distance may leak past this file.

SQLite (store.py) is the source of truth; this collection is a derived
index, rebuilt from it via `reindex()`.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# Belt and braces (PLAN.md): Settings(anonymized_telemetry=False) below is
# the primary switch, but Chroma also reads this env var directly in a few
# telemetry code paths — set both so a stray Settings omission can't
# silently reintroduce a network call. Must be set before chromadb's own
# telemetry module is imported, so this sits above the `import chromadb`.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")

import chromadb  # noqa: E402 - must follow the telemetry env var above
from chromadb.config import Settings  # noqa: E402

from omen import config, errors  # noqa: E402
from omen.config import CHROMA_COLLECTION, EMBED_MODEL  # noqa: E402
from omen.contracts import Incident  # noqa: E402

# None means "use config.CHROMA_PATH" (read at call time, so --store and
# OMEN_HOME apply). Tests set this to a temporary directory.
CHROMA_PATH: Path | None = None


def _chroma_path() -> Path:
    return Path(CHROMA_PATH) if CHROMA_PATH else config.CHROMA_PATH


def _embed(prompts: list[str]) -> list[list[float]]:
    """Call Ollama /api/embed directly. PLAN.md: "Embeddings never touch
    ADK or LiteLLM" — routing them through the agent framework adds layers
    with nothing to gain."""
    if not prompts:
        return []
    body = json.dumps({"model": EMBED_MODEL, "input": prompts}).encode()
    req = urllib.request.Request(
        f"{config.OLLAMA_HOST}/api/embed", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:  # Ollama answers 404 for a model that is not pulled
            raise errors.ModelMissing(EMBED_MODEL) from e
        raise
    except urllib.error.URLError as e:
        raise errors.OllamaUnavailable() from e
    return data["embeddings"]


def embed_query(texts: list[str]) -> list[list[float]]:
    """EmbeddingGemma retrieval-query prefix. PLAN.md Phase 3: incident
    variants get retrieval-query, code chunks get retrieval-document —
    swapping them silently costs recall."""
    return _embed([f"task: search result | query: {t}" for t in texts])


def embed_document(texts: list[str], title: str = "none") -> list[list[float]]:
    """EmbeddingGemma retrieval-document prefix. Used for code chunks
    (librarian.py), not here — kept alongside embed_query since both hit
    the same Ollama endpoint and prefix convention."""
    return _embed([f"title: {title} | text: {t}" for t in texts])


def get_client() -> chromadb.ClientAPI:
    path = _chroma_path()
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(path),
        settings=Settings(anonymized_telemetry=False),
    )


def get_collection(client: chromadb.ClientAPI | None = None):
    client = client or get_client()
    # embedding_function=None is load-bearing: the chromadb default
    # instantiates ONNXMiniLM_L6_V2 and downloads it from S3 on first use.
    return client.get_or_create_collection(
        CHROMA_COLLECTION,
        embedding_function=None,
        metadata={"hnsw:space": "cosine"},
    )


def _variants(incident: Incident) -> list[tuple[str, str, str]]:
    """(id, variant_label, text) for one incident: the mechanism sentence
    plus each surface form, each embedded and indexed separately so an
    incident's score is the max over its variants."""
    out = [(f"{incident.ref}::mechanism", "mechanism", incident.failure_mechanism)]
    for i, form in enumerate(incident.surface_forms):
        out.append((f"{incident.ref}::surface:{i}", f"surface:{i}", form))
    return out


def _records(incidents: list[Incident]) -> tuple[list[str], list[str], list[dict]]:
    """(ids, texts, metadatas) for every variant of `incidents`."""
    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []
    for incident in incidents:
        for vid, label, text in _variants(incident):
            ids.append(vid)
            texts.append(text)
            metadatas.append(
                {
                    "incident_ref": incident.ref,
                    "incident_id": incident.id,
                    "variant": label,
                    "kind": incident.kind,
                    "severity": incident.severity or "",
                }
            )
    return ids, texts, metadatas


def reindex(incidents: list[Incident]) -> int:
    """Rebuild the Chroma collection from SQLite (`omen reindex`, and
    `omen doctor --fix`). Returns the number of variants indexed."""
    client = get_client()
    try:
        client.delete_collection(CHROMA_COLLECTION)
    except Exception:
        pass  # first run: collection doesn't exist yet
    col = get_collection(client)

    ids, texts, metadatas = _records(incidents)
    if not ids:
        return 0

    embeddings = embed_query(texts)
    col.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    return len(ids)


def reindex_incident(incident: Incident) -> int:
    """Re-embed ONE incident's variants (audit B26): delete its variants by
    `incident_ref`, upsert the new ones. Used by `write_incident` and
    `omen memory revert`; the rest of the collection is untouched, so a
    write costs one incident's embeddings, not the whole ledger's. The
    embedding happens first, so a failed embed leaves the old variants in
    place. Returns the number of variants indexed."""
    ids, texts, metadatas = _records([incident])
    embeddings = embed_query(texts)
    col = get_collection()
    col.delete(where={"incident_ref": incident.ref})
    col.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    return len(ids)


def indexed_documents() -> dict[str, str]:
    """{variant id: document} for everything in the collection, without
    creating a store that does not exist (`omen doctor` is read-only)."""
    if not _chroma_path().exists():
        return {}
    got = get_collection().get(include=["documents"])
    return dict(zip(got["ids"], got["documents"]))


def count() -> int:
    return get_collection().count()


def delete_incident_variants(ref: str) -> None:
    get_collection().delete(where={"incident_ref": ref})


def query_chunks(embeddings: list[list[float]], n_results: int = 6) -> list[list[dict]]:
    """Query with many chunk embeddings in ONE Chroma call (production plan
    Phase 5: one query for a whole scan, not one per chunk). Returns, per
    embedding and in order, hits with `similarity` already converted from
    Chroma's raw cosine distance (PLAN.md: "convert once at the vectors.py
    boundary so no threshold logic downstream sees a raw distance") —
    callers never see `distance`."""
    if not embeddings:
        return []
    res = get_collection().query(query_embeddings=embeddings, n_results=n_results)
    all_hits = []
    for ids, dists, metas, docs in zip(res["ids"], res["distances"], res["metadatas"], res["documents"]):
        all_hits.append(
            [
                {"id": id_, "similarity": 1.0 - dist, "metadata": meta, "document": doc}
                for id_, dist, meta, doc in zip(ids, dists, metas, docs)
            ]
        )
    return all_hits


def query_chunk(embedding: list[float], n_results: int = 6) -> list[dict]:
    """Query with one chunk embedding (see `query_chunks`)."""
    return query_chunks([embedding], n_results=n_results)[0]
