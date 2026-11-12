"""SQLite: the source of truth for the incident ledger, the chunk-vector
cache, run/finding/tool-call telemetry. Chroma (vectors.py) is a derived
index rebuilt from this file via `omen reindex` — never the other way
around.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
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
    (
        3,  # Phase 4: incident history and undo (B8), provenance list, finding stop reason
        [
            "ALTER TABLE incidents ADD COLUMN sources TEXT",
            """CREATE TABLE incident_history (
                 id       INTEGER PRIMARY KEY,
                 ref      TEXT NOT NULL,
                 version  INTEGER NOT NULL,
                 ts       TEXT NOT NULL DEFAULT (datetime('now')),
                 action   TEXT NOT NULL,
                 snapshot TEXT NOT NULL,
                 source   TEXT,
                 proposal TEXT,
                 UNIQUE (ref, version)
               )""",
            "ALTER TABLE findings ADD COLUMN stopped_reason TEXT",
        ],
    ),
    (
        4,  # Phase 5: chunks skipped by --max-gated / --max-positives are stored, never dropped
        [
            """CREATE TABLE skipped_chunks (
                 id             INTEGER PRIMARY KEY,
                 run_id         TEXT NOT NULL REFERENCES runs(run_id),
                 file_path      TEXT NOT NULL,
                 start_line     INTEGER,
                 end_line       INTEGER,
                 symbol         TEXT,
                 incident_ref   TEXT,
                 similarity     REAL,
                 triage_verdict TEXT,
                 stage          TEXT NOT NULL,
                 reason         TEXT NOT NULL
               )""",
            "ALTER TABLE runs ADD COLUMN n_skipped INTEGER",
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
        sources=_load_sources(row),
    )


def _load_sources(row: sqlite3.Row) -> list[dict]:
    """The `sources` JSON column; for rows written before schema v3 (NULL)
    the single provenance they always had."""
    if row["sources"]:
        return json.loads(row["sources"])
    return [{"source": row["source"], "learned_by": row["learned_by"] or "seed", "added_at": row["created_at"]}]


def _add_source(existing: list[dict], source: str | None, learned_by: str) -> list[dict]:
    """Append a provenance unless that exact (source, learned_by) is already listed."""
    if any(s.get("source") == source and s.get("learned_by") == learned_by for s in existing):
        return [dict(s) for s in existing]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [*[dict(s) for s in existing], {"source": source, "learned_by": learned_by, "added_at": now}]


# ---------------------------------------------------------------------------
# History: every create / update / forget / revert is a row (production plan
# Phase 4). `snapshot` is the incident's full state AFTER the action (for a
# forget: its state at the moment it was forgotten), so any row can be
# restored by `revert_incident`. `proposal` holds text a merge did not apply.
# ---------------------------------------------------------------------------

SNAPSHOT_FIELDS = (
    "ref", "kind", "title", "failure_mechanism", "what_happened", "the_rule",
    "severity", "occurred_on", "source", "learned_by", "created_at",
)
MAX_SURFACE_FORMS = 12


def incident_snapshot(incident: Incident) -> dict:
    snap = {f: getattr(incident, f) for f in SNAPSHOT_FIELDS}
    snap["surface_forms"] = list(incident.surface_forms)
    snap["sources"] = [dict(s) for s in incident.sources]
    return snap


def _latest_version(conn: sqlite3.Connection, ref: str) -> int | None:
    row = conn.execute("SELECT MAX(version) AS v FROM incident_history WHERE ref = ?", (ref,)).fetchone()
    return row["v"]


def _record_history(
    conn: sqlite3.Connection, ref: str, action: str, snapshot: dict, source: str | None,
    proposal: dict | None = None, ts: str | None = None,
) -> int:
    version = (_latest_version(conn, ref) or 0) + 1
    conn.execute(
        """INSERT INTO incident_history (ref, version, ts, action, snapshot, source, proposal)
           VALUES (?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?)""",
        (ref, version, ts, action, json.dumps(snapshot), source,
         json.dumps(proposal) if proposal is not None else None),
    )
    return version


def _ensure_baseline(conn: sqlite3.Connection, incident: Incident) -> None:
    """An incident written before schema v3 has no history. Before its first
    change, record its current state as version 1 (stamped with its
    creation time), so that change has something to be reverted to."""
    if _latest_version(conn, incident.ref) is None:
        _record_history(
            conn, incident.ref, "create", incident_snapshot(incident),
            incident.source or incident.learned_by, ts=incident.created_at,
        )


def list_history(conn: sqlite3.Connection, ref: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT version, ts, action, source, proposal IS NOT NULL AS has_proposal "
        "FROM incident_history WHERE ref = ? ORDER BY version",
        (ref,),
    ).fetchall()


def get_history_snapshot(conn: sqlite3.Connection, ref: str, version: int) -> dict | None:
    row = conn.execute(
        "SELECT snapshot FROM incident_history WHERE ref = ? AND version = ?", (ref, version)
    ).fetchone()
    return json.loads(row["snapshot"]) if row else None


def get_history_proposal(conn: sqlite3.Connection, ref: str, version: int) -> dict | None:
    row = conn.execute(
        "SELECT proposal FROM incident_history WHERE ref = ? AND version = ?", (ref, version)
    ).fetchone()
    return json.loads(row["proposal"]) if row and row["proposal"] else None


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
    """Insert a new incident or, if `ref` already exists, REPLACE its
    fields and surface forms with `seed`'s. This is the curated-input path
    (`omen seed`, and creating a new incident in `write_incident`). The
    Archivist updating an existing incident goes through `merge_incident`
    instead, which does not overwrite curated text (audit B8).

    Writes a history row (`create`, or `update` if anything changed); the
    provenance list keeps every earlier source and adds this one."""
    existing = conn.execute("SELECT * FROM incidents WHERE ref = ?", (seed.ref,)).fetchone()
    before = _row_to_incident(conn, existing) if existing else None
    if before:
        _ensure_baseline(conn, before)
    sources = _add_source(before.sources if before else [], seed.source, seed.learned_by)

    if before is None:
        cur = conn.execute(
            """INSERT INTO incidents
               (ref, kind, title, failure_mechanism, what_happened, the_rule,
                severity, occurred_on, source, learned_by, sources)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                seed.ref, seed.kind, seed.title, seed.failure_mechanism,
                seed.what_happened, seed.the_rule, seed.severity,
                seed.occurred_on, seed.source, seed.learned_by, json.dumps(sources),
            ),
        )
        incident_id = cur.lastrowid
        _bump_ref_high_water(conn, seed.ref)
    else:
        incident_id = existing["id"]
        conn.execute(
            """UPDATE incidents SET
                 kind = ?, title = ?, failure_mechanism = ?, what_happened = ?,
                 the_rule = ?, severity = ?, occurred_on = ?, source = ?, learned_by = ?, sources = ?
               WHERE id = ?""",
            (
                seed.kind, seed.title, seed.failure_mechanism, seed.what_happened,
                seed.the_rule, seed.severity, seed.occurred_on, seed.source,
                seed.learned_by, json.dumps(sources), incident_id,
            ),
        )
        conn.execute("DELETE FROM surface_forms WHERE incident_id = ?", (incident_id,))

    conn.executemany(
        "INSERT INTO surface_forms (incident_id, form) VALUES (?, ?)",
        [(incident_id, form) for form in seed.surface_forms],
    )
    after = _row_to_incident(conn, conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone())
    if before is None:
        _record_history(conn, after.ref, "create", incident_snapshot(after), seed.source or seed.learned_by)
    elif incident_snapshot(after) != incident_snapshot(before):  # re-seeding unchanged data adds no row
        _record_history(conn, after.ref, "update", incident_snapshot(after), seed.source or seed.learned_by)
    conn.commit()
    return after


def _norm_form(form: str) -> str:
    return " ".join(form.split()).casefold()


def merge_surface_forms(existing: list[str], proposed: list[str], cap: int = MAX_SURFACE_FORMS) -> list[str]:
    """The union of two surface-form lists. Existing forms come first and
    are never dropped (they are the curated ones, even if there are
    already more than `cap`); proposed forms are added in order, skipping
    any that equals an earlier form after collapsing whitespace and case,
    until the list holds `cap` forms."""
    merged = list(existing)
    seen = {_norm_form(f) for f in merged}
    for form in proposed:
        clean = " ".join(form.split())
        if not clean or _norm_form(clean) in seen:
            continue
        if len(merged) >= cap:
            break
        merged.append(clean)
        seen.add(_norm_form(clean))
    return merged


def merge_incident(conn: sqlite3.Connection, ref: str, seed: IncidentSeed) -> Incident:
    """The Archivist found a near-duplicate and proposes `seed` as an update
    to `ref`. Merge, do not overwrite (audit B8):

    - surface forms: union (see `merge_surface_forms`), existing first;
    - title, failure_mechanism, what_happened, the_rule: KEPT. They are
      curated text; a model's rephrase must not replace them;
    - severity, occurred_on: kept; filled from `seed` only if blank;
    - learned_by / source (the original provenance): kept; `seed`'s source
      is appended to `sources`.

    The proposed text that was not applied is saved in the history row's
    `proposal`, so nothing the model wrote is lost. Raises LookupError if
    `ref` does not exist.
    """
    row = conn.execute("SELECT * FROM incidents WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        raise LookupError(f"no incident {ref}")
    before = _row_to_incident(conn, row)
    _ensure_baseline(conn, before)

    forms = merge_surface_forms(before.surface_forms, seed.surface_forms)
    conn.executemany(
        "INSERT INTO surface_forms (incident_id, form) VALUES (?, ?)",
        [(before.id, f) for f in forms[len(before.surface_forms):]],  # existing are kept in place, in order
    )
    conn.execute(
        "UPDATE incidents SET severity = ?, occurred_on = ?, sources = ? WHERE id = ?",
        (
            before.severity or seed.severity,
            before.occurred_on or seed.occurred_on,
            json.dumps(_add_source(before.sources, seed.source, seed.learned_by)),
            before.id,
        ),
    )
    after = _row_to_incident(conn, conn.execute("SELECT * FROM incidents WHERE id = ?", (before.id,)).fetchone())

    unapplied = {
        k: getattr(seed, k)
        for k in ("title", "failure_mechanism", "what_happened", "the_rule")
        if getattr(seed, k) != getattr(before, k)
    }
    if unapplied:
        unapplied["surface_forms"] = list(seed.surface_forms)
        unapplied["severity"] = seed.severity
    if unapplied or incident_snapshot(after) != incident_snapshot(before):
        _record_history(
            conn, ref, "update", incident_snapshot(after), seed.source or seed.learned_by,
            proposal=unapplied or None,
        )
    conn.commit()
    return after


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


def forget_incident(conn: sqlite3.Connection, ref: str, source: str = "omen memory forget") -> bool:
    """Delete an incident and its surface forms (cascades via FK). This is
    the undo for autonomous memory writes (PLAN.md: "omen memory forget").
    The ref's number is retired, never reassigned (audit B11).

    History is KEPT: a `forget` row snapshots the incident as it was, so
    forgetting is itself undoable with `revert_incident` (production plan
    Phase 4). Nothing in Omen deletes history."""
    row = conn.execute("SELECT * FROM incidents WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        return False
    before = _row_to_incident(conn, row)
    _ensure_baseline(conn, before)
    conn.execute("DELETE FROM incidents WHERE ref = ?", (ref,))
    _bump_ref_high_water(conn, ref)
    _record_history(conn, ref, "forget", incident_snapshot(before), source)
    conn.commit()
    return True


def revert_incident(conn: sqlite3.Connection, ref: str, to: int | None = None) -> tuple[Incident, int]:
    """Restore `ref` from a history snapshot. Returns (incident, version
    restored). Works on a forgotten incident too: it comes back under the
    same ref (refs are never reused, so the number is still free).

    Default target: for an existing incident, the version before its latest
    change (undo the last change); for a forgotten one, its state when
    forgotten. `to` names a version explicitly. The revert is itself
    recorded as a new history row, so it can be undone too. The caller
    must re-index the returned incident (`vectors.reindex_incident`).

    Raises LookupError if there is no such history, ValueError if there is
    nothing earlier to go back to.
    """
    latest = _latest_version(conn, ref)
    if latest is None:
        raise LookupError(f"no history for {ref}")
    row = conn.execute("SELECT * FROM incidents WHERE ref = ?", (ref,)).fetchone()
    if to is None:
        target = latest if row is None else latest - 1
        if target < 1:
            raise ValueError(f"{ref} has no earlier version to revert to (use `omen memory forget` to remove it)")
    else:
        target = to
    snap = get_history_snapshot(conn, ref, target)
    if snap is None:
        raise LookupError(f"{ref} has no version {target}")

    fields = [snap[f] for f in ("kind", "title", "failure_mechanism", "what_happened", "the_rule",
                                "severity", "occurred_on", "source", "learned_by")]
    if row is None:
        cur = conn.execute(
            """INSERT INTO incidents (ref, kind, title, failure_mechanism, what_happened, the_rule,
                                      severity, occurred_on, source, learned_by, created_at, sources)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, *fields, snap["created_at"], json.dumps(snap["sources"])),
        )
        incident_id = cur.lastrowid
    else:
        incident_id = row["id"]
        conn.execute(
            """UPDATE incidents SET kind = ?, title = ?, failure_mechanism = ?, what_happened = ?, the_rule = ?,
                                    severity = ?, occurred_on = ?, source = ?, learned_by = ?, sources = ?
               WHERE id = ?""",
            (*fields, json.dumps(snap["sources"]), incident_id),
        )
        conn.execute("DELETE FROM surface_forms WHERE incident_id = ?", (incident_id,))
    conn.executemany(
        "INSERT INTO surface_forms (incident_id, form) VALUES (?, ?)",
        [(incident_id, f) for f in snap["surface_forms"]],
    )
    after = _row_to_incident(conn, conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone())
    _record_history(conn, ref, "revert", incident_snapshot(after), f"revert to v{target}")
    conn.commit()
    return after, target


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
            confidence, tool_calls, downgrade_reason, stopped_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            finding.run_id, finding.incident_ref, finding.file_path,
            finding.start_line, finding.end_line, finding.symbol,
            finding.similarity, finding.verdict, finding.code_mechanism,
            finding.reasoning, finding.evidence, finding.confidence,
            finding.tool_calls, finding.downgrade_reason, finding.stopped_reason,
        ),
    )
    conn.commit()
    return cur.lastrowid


def record_skipped_chunk(conn: sqlite3.Connection, skipped) -> int:
    cur = conn.execute(
        """INSERT INTO skipped_chunks
           (run_id, file_path, start_line, end_line, symbol, incident_ref,
            similarity, triage_verdict, stage, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            skipped.run_id, skipped.file_path, skipped.start_line, skipped.end_line,
            skipped.symbol, skipped.incident_ref, skipped.similarity,
            skipped.triage_verdict, skipped.stage, skipped.reason,
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
