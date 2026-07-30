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
import urllib.request

# Belt and braces (PLAN.md): Settings(anonymized_telemetry=False) below is
# the primary switch, but Chroma also reads this env var directly in a few
# telemetry code paths — set both so a stray Settings omission can't
# silently reintroduce a network call. Must be set before chromadb's own
# telemetry module is imported, so this sits above the `import chromadb`.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")

import chromadb
from chromadb.config import Settings

from omen.config import CHROMA_COLLECTION, CHROMA_PATH, EMBED_MODEL, OLLAMA_HOST
from omen.contracts import Incident

_EMBED_URL = f"{OLLAMA_HOST}/api/embed"


def _embed(prompts: list[str]) -> list[list[float]]:
    """Call Ollama /api/embed directly. PLAN.md: "Embeddings never touch
    ADK or LiteLLM" — routing them through the agent framework adds layers
    with nothing to gain."""
    if not prompts:
        return []
    body = json.dumps({"model": EMBED_MODEL, "input": prompts}).encode()
    req = urllib.request.Request(
        _EMBED_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
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
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(CHROMA_PATH),
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


def reindex(incidents: list[Incident]) -> int:
    """Rebuild the Chroma collection from SQLite. Deterministic ids mean
    re-seeding upserts rather than duplicating. Returns the number of
    variants indexed."""
    client = get_client()
    try:
        client.delete_collection(CHROMA_COLLECTION)
    except Exception:
        pass  # first run: collection doesn't exist yet
    col = get_collection(client)

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

    if not ids:
        return 0

    embeddings = embed_query(texts)
    col.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    return len(ids)


def count() -> int:
    return get_collection().count()


def delete_incident_variants(ref: str) -> None:
    get_collection().delete(where={"incident_ref": ref})


def query_chunk(embedding: list[float], n_results: int = 6) -> list[dict]:
    """Query with a chunk embedding. Returns hits with `similarity` already
    converted from Chroma's raw cosine distance (PLAN.md: "convert once at
    the vectors.py boundary so no threshold logic downstream sees a raw
    distance") — callers never see `distance`."""
    col = get_collection()
    res = col.query(query_embeddings=[embedding], n_results=n_results)
    hits = []
    for id_, dist, meta, doc in zip(
        res["ids"][0], res["distances"][0], res["metadatas"][0], res["documents"][0]
    ):
        hits.append({"id": id_, "similarity": 1.0 - dist, "metadata": meta, "document": doc})
    return hits
