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

    assert store.schema_version(conn) == store.SCHEMA_VERSION == 4
    assert {"note"} <= _columns(conn, "tool_calls")
    assert {"ms_scout", "ms_adjudicate", "n_written"} <= _columns(conn, "runs")
    assert {"downgrade_reason", "stopped_reason"} <= _columns(conn, "findings")
    assert "sources" in _columns(conn, "incidents")
    assert {"ref", "version", "ts", "action", "snapshot", "source", "proposal"} <= _columns(conn, "incident_history")
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
    assert store.schema_version(conn) == 4
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


# ---------------------------------------------------------------------------
# Version 2 -> 3 (Phase 4): incident history, provenance list, stopped_reason
# ---------------------------------------------------------------------------


def _make_v2_db(path):
    """A real version-2 database: built by the real migrations, stopped at 2,
    then filled with rows."""
    real = store.MIGRATIONS
    store.MIGRATIONS = [m for m in real if m[0] <= 2]
    try:
        conn = store.connect(path)
    finally:
        store.MIGRATIONS = real
    conn.executescript(OLD_ROWS)
    conn.execute("INSERT INTO meta (key, value) VALUES ('ref_high_water', '4')")
    conn.execute("UPDATE findings SET downgrade_reason = 'kept' WHERE run_id = 'r1'")
    conn.execute("UPDATE runs SET ms_scout = 5, n_written = 2 WHERE run_id = 'r1'")
    conn.commit()
    assert store.schema_version(conn) == 2
    conn.close()


def test_version_2_database_upgrades_to_latest_without_losing_rows(tmp_path):
    db = tmp_path / "v2.db"
    _make_v2_db(db)

    conn = store.connect(db)

    assert store.schema_version(conn) == 4
    assert _counts(conn) == {
        "incidents": 1, "surface_forms": 2, "chunk_vectors": 1, "runs": 1, "findings": 1, "tool_calls": 1,
    }
    # data written under version 2 is untouched
    assert conn.execute("SELECT downgrade_reason, stopped_reason FROM findings").fetchone()[:] == ("kept", None)
    assert conn.execute("SELECT ms_scout, n_written FROM runs").fetchone()[:] == (5, 2)
    assert store.ref_high_water(conn) == 4
    # an old incident reads with its one original provenance, and has no history yet
    inc = store.get_incident(conn, "OMEN-001")
    assert [(s["learned_by"], s["source"]) for s in inc.sources] == [("seed", None)]
    assert conn.execute("SELECT COUNT(*) FROM incident_history").fetchone()[0] == 0
    # and its first change gets a baseline version 1 to revert to
    store.forget_incident(conn, "OMEN-001")
    assert [r["action"] for r in store.list_history(conn, "OMEN-001")] == ["create", "forget"]


def test_a_failing_version_3_migration_rolls_back_cleanly(tmp_path, monkeypatch):
    db = tmp_path / "v2.db"
    _make_v2_db(db)
    good = [m for m in store.MIGRATIONS if m[0] <= 2]
    broken_3 = (3, [
        "ALTER TABLE incidents ADD COLUMN sources TEXT",
        "CREATE TABLE incident_history (id INTEGER PRIMARY KEY, ref TEXT)",
        "ALTER TABLE no_such_table ADD COLUMN x TEXT",
    ])
    monkeypatch.setattr(store, "MIGRATIONS", [*good, broken_3])

    with pytest.raises(sqlite3.OperationalError):
        store.connect(db)

    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    assert "sources" not in _columns(raw, "incidents")  # the first statement was rolled back
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name = 'incident_history'").fetchall()
    assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
    assert raw.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    raw.close()

    monkeypatch.undo()  # with the real migrations the database upgrades fine afterwards
    assert store.schema_version(store.connect(db)) == 4



# ---------------------------------------------------------------------------
# Version 3 -> 4 (Phase 5): skipped_chunks and runs.n_skipped
# ---------------------------------------------------------------------------


def _make_v3_db(path):
    real = store.MIGRATIONS
    store.MIGRATIONS = [m for m in real if m[0] <= 3]
    try:
        conn = store.connect(path)
    finally:
        store.MIGRATIONS = real
    conn.executescript(OLD_ROWS)
    conn.execute("INSERT INTO incident_history (ref, version, action, snapshot) VALUES ('OMEN-001', 1, 'create', '{}')")
    conn.commit()
    assert store.schema_version(conn) == 3
    conn.close()


def test_version_3_database_upgrades_to_4_without_losing_rows(tmp_path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)

    conn = store.connect(db)

    assert store.schema_version(conn) == 4
    assert "n_skipped" in _columns(conn, "runs")
    assert {"run_id", "file_path", "start_line", "end_line", "symbol", "incident_ref", "similarity",
            "triage_verdict", "stage", "reason"} <= _columns(conn, "skipped_chunks")
    assert _counts(conn)["runs"] == 1 and _counts(conn)["findings"] == 1
    assert conn.execute("SELECT COUNT(*) FROM incident_history").fetchone()[0] == 1  # Phase 4 data untouched
    assert conn.execute("SELECT n_skipped FROM runs").fetchone()[0] is None  # runs made before v4 have no count
    # and the new table is usable, with its foreign key to runs enforced
    store.record_skipped_chunk(conn, SimpleSkipped(run_id="r1"))
    assert conn.execute("SELECT COUNT(*) FROM skipped_chunks").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.record_skipped_chunk(conn, SimpleSkipped(run_id="no-such-run"))


class SimpleSkipped:
    def __init__(self, run_id):
        self.run_id, self.file_path, self.start_line, self.end_line = run_id, "a.py", 1, 2
        self.symbol, self.incident_ref, self.similarity = "f", "OMEN-001", 0.5
        self.triage_verdict, self.stage, self.reason = None, "triage", "not triaged: --max-gated 1 reached"


def test_a_failing_version_4_migration_rolls_back_cleanly(tmp_path, monkeypatch):
    db = tmp_path / "v3.db"
    _make_v3_db(db)
    good = [m for m in store.MIGRATIONS if m[0] <= 3]
    broken_4 = (4, [
        "CREATE TABLE skipped_chunks (id INTEGER PRIMARY KEY, run_id TEXT)",
        "ALTER TABLE no_such_table ADD COLUMN x TEXT",
    ])
    monkeypatch.setattr(store, "MIGRATIONS", [*good, broken_4])

    with pytest.raises(sqlite3.OperationalError):
        store.connect(db)

    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name = 'skipped_chunks'").fetchall()
    assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"
    assert raw.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    raw.close()

    monkeypatch.undo()
    assert store.schema_version(store.connect(db)) == 4
