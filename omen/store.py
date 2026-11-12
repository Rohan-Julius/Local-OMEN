"""SQLite: the source of truth for the incident ledger, the chunk-vector
cache, run/finding/tool-call telemetry. Chroma (vectors.py) is a derived
index rebuilt from this file via `omen reindex` — never the other way
around.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import yaml

from omen import config
from omen.config import REF_PREFIX
from omen.contracts import Incident, IncidentSeed, RunRecord, ToolCallRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id                INTEGER PRIMARY KEY,
  ref               TEXT UNIQUE NOT NULL,
  kind              TEXT NOT NULL DEFAULT 'incident',
  title             TEXT NOT NULL,
  failure_mechanism TEXT NOT NULL,
  what_happened     TEXT NOT NULL,
  the_rule          TEXT NOT NULL,
  severity          TEXT,
  occurred_on       TEXT,
  source            TEXT,
  learned_by        TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS surface_forms (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  form TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk_vectors (
  content_hash TEXT NOT NULL, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vec BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (content_hash, model)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, mission TEXT NOT NULL,
  repo_path TEXT, scope TEXT, llm_model TEXT, embed_model TEXT, runner TEXT,
  started_at TEXT, finished_at TEXT,
  n_files INTEGER, n_chunks INTEGER, n_cache_hits INTEGER,
  n_gated INTEGER, n_triaged INTEGER, n_investigated INTEGER,
  n_tool_calls INTEGER, n_confirmed INTEGER,
  ms_embed INTEGER, ms_triage INTEGER, ms_investigate INTEGER, ms_total INTEGER
);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  incident_ref TEXT NOT NULL, file_path TEXT NOT NULL,
  start_line INTEGER, end_line INTEGER, symbol TEXT,
  similarity REAL NOT NULL,
  verdict TEXT NOT NULL,
  code_mechanism TEXT, reasoning TEXT, evidence TEXT, confidence TEXT,
  tool_calls INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  chunk_symbol TEXT, seq INTEGER,
  tool_name TEXT NOT NULL, args_json TEXT, result_summary TEXT, ms INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# meta key: the highest incident ref number ever inserted or forgotten, so
# a forgotten ref is never handed out again (audit B11).
REF_HIGH_WATER_KEY = "ref_high_water"

# Schema versioning. SCHEMA above is version 1, the layout every database
# had before Phase 3 (with or without the `meta` table). Each migration
# moves a database from version n-1 to n and is applied once, in order,
# inside a transaction; `meta.schema_version` records the result. A fresh
# database is created at version 1 by SCHEMA and migrated forward the same
# way, so there is one path for old and new databases. Phase 4 extends
# this list; never edit an applied migration, add a new one.
SCHEMA_VERSION_KEY = "schema_version"
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        2,  # Phase 3: audit trail (B12, B14, B15) and evidence downgrade reason (B6)
        [
            "ALTER TABLE tool_calls ADD COLUMN note TEXT",
            "ALTER TABLE runs ADD COLUMN ms_scout INTEGER",
            "ALTER TABLE runs ADD COLUMN ms_adjudicate INTEGER",
            "ALTER TABLE runs ADD COLUMN n_written INTEGER",
            "ALTER TABLE findings ADD COLUMN downgrade_reason TEXT",
        ],
    ),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (SCHEMA_VERSION_KEY,)).fetchone()
    return int(row["value"]) if row else 1


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to SCHEMA_VERSION. Returns the version it
    started at. Rows are never touched, only columns added."""
    start = current = schema_version(conn)
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        # One explicit transaction per migration: all of it or none of it.
        # (Python 3.11's sqlite3 does not open a transaction before DDL on
        # its own, so `with conn:` alone would let each ALTER autocommit.)
        conn.execute("BEGIN")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION_KEY, str(version)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        current = version
    return start


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the ledger. Default: `config.DB_PATH`,
    read at call time so `--store` / OMEN_HOME apply."""
    db_path = Path(db_path) if db_path else config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


def _row_to_incident(conn: sqlite3.Connection, row: sqlite3.Row) -> Incident:
    forms = conn.execute(
        "SELECT form FROM surface_forms WHERE incident_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    return Incident(
        id=row["id"],
        ref=row["ref"],
        kind=row["kind"],
        title=row["title"],
        failure_mechanism=row["failure_mechanism"],
        what_happened=row["what_happened"],
        the_rule=row["the_rule"],
        severity=row["severity"],
        occurred_on=row["occurred_on"],
        source=row["source"],
        learned_by=row["learned_by"] or "seed",
        created_at=row["created_at"],
        surface_forms=[f["form"] for f in forms],
    )


def ref_number(ref: str) -> int | None:
    """The numeric part of a well-formed ref (`OMEN-012` -> 12), else None."""
    m = re.fullmatch(rf"{re.escape(REF_PREFIX)}-(\d{{3,}})", ref)
    return int(m.group(1)) if m else None


def ref_high_water(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (REF_HIGH_WATER_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def _bump_ref_high_water(conn: sqlite3.Connection, ref: str) -> None:
    """Record `ref` as used. Called on insert and on forget, so the mark
    also covers refs that existed before this table did (seeded or learned
    by an older version) once they are forgotten. Caller commits."""
    n = ref_number(ref)
    if n is not None and n > ref_high_water(conn):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (REF_HIGH_WATER_KEY, str(n)),
        )


def upsert_incident(conn: sqlite3.Connection, seed: IncidentSeed) -> Incident:
    """Insert a new incident or, if `ref` already exists, update it in
    place and replace its surface forms. Used by both `omen seed` and the
    Archivist's `write_incident` tool."""
    existing = conn.execute("SELECT id FROM incidents WHERE ref = ?", (seed.ref,)).fetchone()
    if existing is None:
        cur = conn.execute(
            """INSERT INTO incidents
               (ref, kind, title, failure_mechanism, what_happened, the_rule,
                severity, occurred_on, source, learned_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                seed.ref, seed.kind, seed.title, seed.failure_mechanism,
                seed.what_happened, seed.the_rule, seed.severity,
                seed.occurred_on, seed.source, seed.learned_by,
            ),
        )
        incident_id = cur.lastrowid
        _bump_ref_high_water(conn, seed.ref)
    else:
        incident_id = existing["id"]
        conn.execute(
            """UPDATE incidents SET
                 kind = ?, title = ?, failure_mechanism = ?, what_happened = ?,
                 the_rule = ?, severity = ?, occurred_on = ?, source = ?, learned_by = ?
               WHERE id = ?""",
            (
                seed.kind, seed.title, seed.failure_mechanism, seed.what_happened,
                seed.the_rule, seed.severity, seed.occurred_on, seed.source,
                seed.learned_by, incident_id,
            ),
        )
        conn.execute("DELETE FROM surface_forms WHERE incident_id = ?", (incident_id,))

    conn.executemany(
        "INSERT INTO surface_forms (incident_id, form) VALUES (?, ?)",
        [(incident_id, form) for form in seed.surface_forms],
    )
    conn.commit()
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return _row_to_incident(conn, row)


def seed_from_yaml(conn: sqlite3.Connection, yaml_path: Path) -> list[Incident]:
    data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    seeds = [IncidentSeed(**entry) for entry in data.get("incidents", [])]
    return [upsert_incident(conn, seed) for seed in seeds]


def list_incidents(conn: sqlite3.Connection) -> list[Incident]:
    rows = conn.execute("SELECT * FROM incidents ORDER BY ref").fetchall()
    return [_row_to_incident(conn, row) for row in rows]


def get_incident(conn: sqlite3.Connection, ref: str) -> Incident | None:
    row = conn.execute("SELECT * FROM incidents WHERE ref = ?", (ref,)).fetchone()
    return _row_to_incident(conn, row) if row else None


def forget_incident(conn: sqlite3.Connection, ref: str) -> bool:
    """Delete an incident and its surface forms (cascades via FK). This is
    the undo for autonomous memory writes (PLAN.md: "omen memory forget").
    The ref's number is retired, never reassigned (audit B11)."""
    cur = conn.execute("DELETE FROM incidents WHERE ref = ?", (ref,))
    if cur.rowcount > 0:
        _bump_ref_high_water(conn, ref)
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Chunk-vector cache (exact content-hash lookup, never a similarity search)
# ---------------------------------------------------------------------------


def get_cached_vector(conn: sqlite3.Connection, content_hash: str, model: str) -> list[float] | None:
    row = conn.execute(
        "SELECT dim, vec FROM chunk_vectors WHERE content_hash = ? AND model = ?",
        (content_hash, model),
    ).fetchone()
    if row is None:
        return None
    import struct

    return list(struct.unpack(f"{row['dim']}f", row["vec"]))


def put_cached_vector(conn: sqlite3.Connection, content_hash: str, model: str, vec: list[float]) -> None:
    import struct

    blob = struct.pack(f"{len(vec)}f", *vec)
    conn.execute(
        "INSERT OR REPLACE INTO chunk_vectors (content_hash, model, dim, vec) VALUES (?, ?, ?, ?)",
        (content_hash, model, len(vec), blob),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Runs / findings / tool calls (telemetry — PLAN.md: "you cannot optimize
# latency you don't measure", and the tool_calls table is the audit trail)
# ---------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, run: RunRecord) -> None:
    conn.execute(
        """INSERT INTO runs (run_id, mission, repo_path, scope, llm_model,
                              embed_model, runner, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (run.run_id, run.mission, run.repo_path, run.scope, run.llm_model,
         run.embed_model, run.runner),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: str, **fields) -> None:
    """Update any subset of the run's summary columns and stamp finished_at."""
    fields = {**fields, "finished_at": None}
    set_clause = ", ".join(f"{k} = ?" for k in fields if k != "finished_at")
    set_clause = (set_clause + ", " if set_clause else "") + "finished_at = datetime('now')"
    values = [v for k, v in fields.items() if k != "finished_at"]
    conn.execute(f"UPDATE runs SET {set_clause} WHERE run_id = ?", (*values, run_id))
    conn.commit()


def record_finding(conn: sqlite3.Connection, finding) -> int:
    cur = conn.execute(
        """INSERT INTO findings
           (run_id, incident_ref, file_path, start_line, end_line, symbol,
            similarity, verdict, code_mechanism, reasoning, evidence,
            confidence, tool_calls, downgrade_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            finding.run_id, finding.incident_ref, finding.file_path,
            finding.start_line, finding.end_line, finding.symbol,
            finding.similarity, finding.verdict, finding.code_mechanism,
            finding.reasoning, finding.evidence, finding.confidence,
            finding.tool_calls, finding.downgrade_reason,
        ),
    )
    conn.commit()
    return cur.lastrowid


def record_tool_call(conn: sqlite3.Connection, call: ToolCallRecord) -> int:
    cur = conn.execute(
        """INSERT INTO tool_calls (run_id, chunk_symbol, seq, tool_name, args_json, result_summary, ms, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (call.run_id, call.chunk_symbol, call.seq, call.tool_name,
         call.args_json, call.result_summary, call.ms, call.note),
    )
    conn.commit()
    return cur.lastrowid
