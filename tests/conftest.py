"""Shared test plumbing.

Two test tiers (docs/PRODUCTION_PLAN.md, ground rule 5):
- unmarked tests need no model and must pass everywhere, including CI;
- `@pytest.mark.live` tests need a reachable Ollama. They are skipped here,
  one test at a time, when Ollama is down. This replaces the old
  module-level `pytestmark = skipif(...)`, which also skipped the pure tests
  that happened to share a module with a live one (audit B19).

`seeded_store` gives live tests a temporary ledger + Chroma index seeded
from the real `incidents.yaml`, so they never depend on whatever happens to
be in the developer's `omen_store/` (audit B20).
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from omen import config, store, vectors

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_YAML = PROJECT_ROOT / "incidents.yaml"

_ollama_reachable_cache: bool | None = None


def ollama_reachable(attempts: int = 3, timeout: float = 5.0) -> bool:
    """A CPU-bound Ollama that is still finishing a long generation can take
    more than a second to answer even its root URL. A 1-second probe once
    marked a running Ollama "unreachable" and silently skipped six live
    tests (Phase 3), so probe patiently and more than once."""
    global _ollama_reachable_cache
    if _ollama_reachable_cache is None:
        _ollama_reachable_cache = False
        for _ in range(attempts):
            try:
                urllib.request.urlopen(config.OLLAMA_HOST, timeout=timeout)
                _ollama_reachable_cache = True
                break
            except Exception:
                continue
    return _ollama_reachable_cache


def pytest_collection_modifyitems(config, items):
    live_items = [item for item in items if item.get_closest_marker("live")]
    if not live_items or ollama_reachable():
        return
    skip_live = pytest.mark.skip(reason="live test: Ollama is not reachable on OLLAMA_HOST")
    for item in live_items:
        item.add_marker(skip_live)


@pytest.fixture(scope="module")
def seeded_store(tmp_path_factory):
    """A temporary SQLite ledger and Chroma index seeded from the real
    `incidents.yaml` and reindexed with the real embedding model. Patches
    `vectors.CHROMA_PATH` and `store.connect` for the duration of the
    module, so code under test that calls `store.connect()` with no
    argument lands here, never in `omen_store/`."""
    tmp = tmp_path_factory.mktemp("seeded_store")
    db_path = tmp / "omen.db"
    real_connect = store.connect

    mp = pytest.MonkeyPatch()
    mp.setattr(vectors, "CHROMA_PATH", tmp / "chroma")
    mp.setattr(store, "connect", lambda *a, **k: real_connect(db_path))
    try:
        conn = real_connect(db_path)
        store.seed_from_yaml(conn, INCIDENTS_YAML)
        vectors.reindex(store.list_incidents(conn))
        yield conn
        conn.close()
    finally:
        mp.undo()
