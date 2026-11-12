"""Phase 3 (production plan): schema versioning via `meta.schema_version`
and an ordered migration list (store.MIGRATIONS). An old database must
upgrade in place on `store.connect` without losing a row.
"""
from __future__ import annotations

import sqlite3

import pytest

from omen import store

# The layout before Phase 3, written out literally (not taken from
# store.SCHEMA) so this test keeps describing the real old format even if
# the baseline ever changes. Phase 0 databases had no `meta` table;
# Phase 1-2 databases had it, holding only the ref high-water mark.
OLD_TABLES = """
CREATE TABLE incidents (
  id INTEGER PRIMARY KEY, ref TEXT UNIQUE NOT NULL, kind TEXT NOT NULL DEFAULT 'incident',
  title TEXT NOT NULL, failure_mechanism TEXT NOT NULL, what_happened TEXT NOT NULL,
  the_rule TEXT NOT NULL, severity TEXT, occurred_on TEXT, source TEXT, learned_by TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE surface_forms (
  id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE, form TEXT NOT NULL
);
CREATE TABLE chunk_vectors (
  content_hash TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (content_hash, model)
);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, mission TEXT NOT NULL, repo_path TEXT, scope TEXT, llm_model TEXT, embed_model TEXT,
  runner TEXT, started_at TEXT, finished_at TEXT, n_files INTEGER, n_chunks INTEGER, n_cache_hits INTEGER,
  n_gated INTEGER, n_triaged INTEGER, n_investigated INTEGER, n_tool_calls INTEGER, n_confirmed INTEGER,
  ms_embed INTEGER, ms_triage INTEGER, ms_investigate INTEGER, ms_total INTEGER
);
CREATE TABLE findings (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), incident_ref TEXT NOT NULL,
  file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER, symbol TEXT, similarity REAL NOT NULL,
  verdict TEXT NOT NULL, code_mechanism TEXT, reasoning TEXT, evidence TEXT, confidence TEXT, tool_calls INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE tool_calls (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), chunk_symbol TEXT, seq INTEGER,
  tool_name TEXT NOT NULL, args_json TEXT, result_summary TEXT, ms INTEGER
);
"""
OLD_META = "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"

OLD_ROWS = """
INSERT INTO incidents (id, ref, title, failure_mechanism, what_happened, the_rule) VALUES (1, 'OMEN-001', 't', 'm', 'h', 'r');
INSERT INTO surface_forms (incident_id, form) VALUES (1, 'a form'), (1, 'another form');
INSERT INTO chunk_vectors (content_hash, model, dim, vec) VALUES ('abc', 'embeddinggemma', 1, x'0000803f');
INSERT INTO runs (run_id, mission, ms_total, n_confirmed) VALUES ('r1', 'scan', 1234, 1);
INSERT INTO findings (run_id, incident_ref, file_path, similarity, verdict) VALUES ('r1', 'OMEN-001', 'a.py', 0.5, 'confirmed');
INSERT INTO tool_calls (run_id, seq, tool_name, ms) VALUES ('r1', 1, 'read_code', 7);
"""


def _make_old_db(path, with_meta: bool):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_TABLES + (OLD_META if with_meta else "") + OLD_ROWS)
    if with_meta:
        conn.execute("INSERT INTO meta (key, value) VALUES ('ref_high_water', '9')")
    conn.commit()
    conn.close()


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _counts(conn):
    tables = ["incidents", "surface_forms", "chunk_vectors", "runs", "findings", "tool_calls"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


@pytest.mark.parametrize("with_meta", [False, True], ids=["phase0-no-meta", "phase1-with-meta"])
def test_old_database_upgrades_in_place_without_losing_rows(tmp_path, with_meta):
    db = tmp_path / "old.db"
    _make_old_db(db, with_meta)

    conn = store.connect(db)

    assert store.schema_version(conn) == store.SCHEMA_VERSION == 2
    assert {"note"} <= _columns(conn, "tool_calls")
    assert {"ms_scout", "ms_adjudicate", "n_written"} <= _columns(conn, "runs")
    assert {"downgrade_reason"} <= _columns(conn, "findings")
    assert _counts(conn) == {
        "incidents": 1, "surface_forms": 2, "chunk_vectors": 1, "runs": 1, "findings": 1, "tool_calls": 1,
    }
    assert conn.execute("SELECT ms_total, ms_scout FROM runs").fetchone()[:] == (1234, None)
    assert store.get_incident(conn, "OMEN-001").surface_forms == ["a form", "another form"]
    if with_meta:
        assert store.ref_high_water(conn) == 9  # the B11 mark survives
    conn.close()

    # A second connect is a no-op: nothing re-applied, nothing lost.
    conn = store.connect(db)
    assert store.schema_version(conn) == 2
    assert _counts(conn)["findings"] == 1


def test_fresh_database_is_created_at_the_latest_version(tmp_path):
    conn = store.connect(tmp_path / "fresh.db")
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    assert "note" in _columns(conn, "tool_calls")


def test_a_failing_migration_is_rolled_back(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    _make_old_db(db, with_meta=True)
    broken = [(2, ["ALTER TABLE tool_calls ADD COLUMN note TEXT", "ALTER TABLE no_such_table ADD COLUMN x TEXT"])]
    monkeypatch.setattr(store, "MIGRATIONS", broken)

    with pytest.raises(sqlite3.OperationalError):
        store.connect(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert "note" not in _columns(conn, "tool_calls")  # the first statement was rolled back too
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() is None
