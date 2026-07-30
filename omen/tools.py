"""The 7 tool functions (PLAN.md), plus the orchestrator-side budget that
enforces the three hard caps on an agent's tool loop: max total calls, a
duplicate-call cache, and a wall-clock ceiling. Pure Python, no LLM — every
tool here is callable from a REPL with no model running (Phase 5).

Tool sets are built per mission and per input path, never pooled:
`build_scan_tools` (4), `build_learn_postmortem_tools` (3),
`build_learn_git_tools` (4) — matching PLAN.md's warning that handing one
agent every tool degrades small-model routing accuracy.
"""
from __future__ import annotations

import functools
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable

from omen import librarian, scout, store, vectors
from omen.config import DIFF_LINE_CAP, GREP_MAX_HITS, MAX_TOOL_CALLS, REF_PREFIX, TOOL_WALL_CLOCK_SECONDS
from omen.contracts import IncidentSeed, ToolCallStep

# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


class PathEscapesRoot(Exception):
    """Raised when a tool-supplied path resolves outside its confined root.

    The agent is reading attacker-adjacent content (a scanned repo it did
    not write) and choosing its own paths; a traversal escape would let a
    crafted repo read arbitrary files on the host. Cheap to prevent, so
    prevent it here rather than trust every call site to remember."""


def confine(root: Path, relative_path: str) -> Path:
    root = Path(root).resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PathEscapesRoot(f"{relative_path!r} escapes the confined root {root}")
    return candidate


# ---------------------------------------------------------------------------
# Scan tools: read_code, grep_symbol, search_memory, get_incident
# ---------------------------------------------------------------------------


def _make_read_code(repo_root: Path) -> Callable:
    def read_code(file_path: str, start_line: int, end_line: int) -> str:
        """Read an exact line range from a file in the scanned repo.

        Args:
            file_path: Path to the file, relative to the repo root.
            start_line: First line to read, 1-indexed, inclusive.
            end_line: Last line to read, 1-indexed, inclusive.
        """
        resolved = confine(repo_root, file_path)
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            return f"error reading {file_path}: {e}"
        start = max(1, start_line)
        end = min(len(lines), end_line)
        if start > end:
            return f"(nothing to show: file has {len(lines)} line(s))"
        return "\n".join(lines[start - 1 : end])

    return read_code


def _make_grep_symbol(repo_root: Path) -> Callable:
    def grep_symbol(symbol: str) -> list[str]:
        """Find definitions and call sites of a symbol. Returns 'path:line: text'.

        Args:
            symbol: The exact function, class, or variable name to search for.
        """
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        hits: list[str] = []
        for path in sorted(Path(repo_root).resolve().rglob("*.py")):
            rel = path.relative_to(Path(repo_root).resolve())
            if any(part in scout.SKIP_DIR_NAMES or part.startswith(".") for part in rel.parts[:-1]):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
                    if len(hits) >= GREP_MAX_HITS:
                        return hits
        return hits

    return grep_symbol


def _make_search_memory(conn: sqlite3.Connection) -> Callable:
    def search_memory(query: str) -> list[str]:
        """Semantic search the incident ledger. Returns 'REF - title - mechanism'.

        Args:
            query: A natural-language description of the failure mechanism to search for.
        """
        # Incident variants are indexed with the retrieval-query prefix
        # (vectors.embed_query); a natural-language probe against them
        # takes the retrieval-document role, mirroring how a code chunk
        # is embedded when matched against the same vectors.
        embedding = vectors.embed_document([query])[0]
        hits = vectors.query_chunk(embedding, n_results=librarian.QUERY_N_RESULTS)
        candidates = librarian.collapse_to_incidents(hits)
        results = []
        for candidate in candidates:
            incident = store.get_incident(conn, candidate.incident_ref)
            if incident:
                results.append(f"{incident.ref} - {incident.title} - {incident.failure_mechanism}")
        return results

    return search_memory


def _make_get_incident(conn: sqlite3.Connection) -> Callable:
    def get_incident(incident_ref: str) -> str:
        """Full record for one incident: mechanism, what happened, the rule.

        Args:
            incident_ref: The incident reference, e.g. 'OMEN-001'.
        """
        incident = store.get_incident(conn, incident_ref)
        if incident is None:
            return f"no incident found with ref {incident_ref!r}"
        return (
            f"{incident.ref}: {incident.title}\n"
            f"mechanism: {incident.failure_mechanism}\n"
            f"what happened: {incident.what_happened}\n"
            f"the rule: {incident.the_rule}"
        )

    return get_incident


def build_scan_tools(repo_root: Path, conn: sqlite3.Connection) -> dict[str, Callable]:
    return {
        "read_code": _make_read_code(repo_root),
        "grep_symbol": _make_grep_symbol(repo_root),
        "search_memory": _make_search_memory(conn),
        "get_incident": _make_get_incident(conn),
    }


# ---------------------------------------------------------------------------
# Learn tools: read_file, read_commit, read_diff, write_incident
# ---------------------------------------------------------------------------


def _make_read_file(allowed_root: Path) -> Callable:
    def read_file(file_path: str) -> str:
        """Read a postmortem or design document from disk.

        Args:
            file_path: Path to the document, relative to the project root.
        """
        resolved = confine(allowed_root, file_path)
        try:
            return resolved.read_text(encoding="utf-8")
        except OSError as e:
            return f"error reading {file_path}: {e}"

    return read_file


def _make_read_commit(repo_root: Path) -> Callable:
    def read_commit(sha: str) -> str:
        """Commit message, author, date, and changed-file stat summary.

        Args:
            sha: The commit SHA (full or abbreviated).
        """
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", "--stat", "--format=%H%n%an <%ae>%n%ad%n%s%n%n%b", sha],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"error: could not read commit {sha}: {result.stderr.strip()}"
        return result.stdout

    return read_commit


def _make_read_diff(repo_root: Path) -> Callable:
    def read_diff(sha: str, file_path: str) -> str:
        """Unified diff for one file in one commit, both sides, truncated to a line cap.

        Args:
            sha: The commit SHA (full or abbreviated).
            file_path: Path to the file within the commit, relative to the repo root.
        """
        # No confine() here: git resolves file_path against its own object
        # database for that commit, not the host filesystem, so a
        # traversal attempt just fails to match a tracked path rather
        # than reading anything outside the repo.
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", sha, "--", file_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"error: could not read diff for {file_path} at {sha}: {result.stderr.strip()}"
        lines = result.stdout.splitlines()
        if len(lines) > DIFF_LINE_CAP:
            lines = lines[:DIFF_LINE_CAP] + [f"... truncated at {DIFF_LINE_CAP} lines ..."]
        return "\n".join(lines)

    return read_diff


def _next_ref(conn: sqlite3.Connection) -> str:
    max_n = 0
    for incident in store.list_incidents(conn):
        m = re.match(rf"{re.escape(REF_PREFIX)}-(\d+)$", incident.ref)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{REF_PREFIX}-{max_n + 1:03d}"


def _make_write_incident(conn: sqlite3.Connection, learned_by: str, source: str) -> Callable:
    def write_incident(
        title: str,
        failure_mechanism: str,
        what_happened: str,
        the_rule: str,
        surface_forms: list[str],
        severity: str = "",
        existing_ref: str = "",
    ) -> str:
        """Create or update a ledger entry, then reindex. Returns the assigned ref.

        Args:
            title: Short human-readable title for the incident.
            failure_mechanism: The failure mechanism, abstracted off its original technology.
            what_happened: What happened in this specific case.
            the_rule: The rule a future scan should check code against.
            surface_forms: 3-5 concrete manifestations of the mechanism, spanning technologies this incident never used.
            severity: Severity if known, e.g. 'sev1', 'sev2', 'sev3'. Empty string if unknown.
            existing_ref: If updating a near-duplicate found via search_memory, its ref (e.g. 'OMEN-003'). Empty string to create a new incident.
        """
        ref = existing_ref or _next_ref(conn)
        seed = IncidentSeed(
            ref=ref,
            title=title,
            failure_mechanism=failure_mechanism,
            what_happened=what_happened,
            the_rule=the_rule,
            surface_forms=list(surface_forms),
            severity=severity or None,
            source=source,
            learned_by=learned_by,
        )
        store.upsert_incident(conn, seed)
        vectors.reindex(store.list_incidents(conn))
        return ref

    return write_incident


def build_learn_postmortem_tools(conn: sqlite3.Connection, allowed_root: Path, source: str) -> dict[str, Callable]:
    return {
        "read_file": _make_read_file(allowed_root),
        "search_memory": _make_search_memory(conn),
        "write_incident": _make_write_incident(conn, learned_by="archivist:postmortem", source=source),
    }


def build_learn_git_tools(conn: sqlite3.Connection, repo_root: Path, source: str) -> dict[str, Callable]:
    return {
        "read_commit": _make_read_commit(repo_root),
        "read_diff": _make_read_diff(repo_root),
        "search_memory": _make_search_memory(conn),
        "write_incident": _make_write_incident(conn, learned_by="archivist:git", source=source),
    }


# ---------------------------------------------------------------------------
# Tool budget: the three hard caps, enforced in the orchestrator
# ---------------------------------------------------------------------------


class ToolBudget:
    """PLAN.md: "an agent loop is an unbounded loop." Enforces, per
    investigation: a max total call count, a duplicate-call cache (a
    repeat of the same tool+args is answered from cache with a note
    instead of re-executing), and a wall-clock ceiling. All three checked
    here, never left to a prompt asking the model to behave."""

    def __init__(self, max_calls: int = MAX_TOOL_CALLS, wall_clock_seconds: float = TOOL_WALL_CLOCK_SECONDS):
        self.max_calls = max_calls
        self.wall_clock_seconds = wall_clock_seconds
        self._start = time.monotonic()
        self._count = 0
        self._cache: dict[tuple, object] = {}

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    @staticmethod
    def _key(tool_name: str, kwargs: dict) -> tuple:
        # json.dumps rather than tuple(sorted(kwargs.items())): a tool
        # argument can itself be a list (write_incident's surface_forms),
        # which is unhashable and breaks a plain tuple/dict key. Every tool
        # argument in this module is JSON-serializable, so this is a safe
        # and still order-independent (sort_keys) cache key.
        return (tool_name, json.dumps(kwargs, sort_keys=True))

    def invoke(self, tools: dict[str, Callable], tool_name: str, kwargs: dict) -> tuple[bool, object, str | None]:
        """Returns (ok, result, note). `ok=False` means the tool did not
        run (a cap tripped); `note` is non-None for anything the caller
        should feed back to the model (a cap message or a duplicate-call
        notice)."""
        if self.elapsed_seconds() > self.wall_clock_seconds:
            return False, None, "wall-clock ceiling exceeded - stop calling tools and answer now"
        if self._count >= self.max_calls:
            return False, None, f"max {self.max_calls} tool calls exceeded - stop calling tools and answer now"

        key = self._key(tool_name, kwargs)
        self._count += 1

        if key in self._cache:
            return True, self._cache[key], "repeat call - same result as before, already retrieved above"

        if tool_name not in tools:
            return False, None, f"unknown tool {tool_name!r}"

        try:
            result = tools[tool_name](**kwargs)
        except PathEscapesRoot as e:
            result = f"error: {e}"
        except Exception as e:
            result = f"error: {type(e).__name__}: {e}"

        self._cache[key] = result
        return True, result, None


def _stringify(result: object) -> str:
    return result if isinstance(result, str) else json.dumps(result)


def budgeted_tools(
    tools: dict[str, Callable],
    budget: ToolBudget,
    on_step: Callable[[ToolCallStep], None] | None = None,
) -> tuple[list[Callable], list[ToolCallStep]]:
    """Wrap each tool so every invocation goes through `budget` (caps,
    dedup cache, error handling) and is recorded into the returned
    transcript list. The wrapper preserves the original signature and
    docstring via functools.wraps, so it's usable as-is by both ADK's
    `LlmAgent(tools=...)` and `ollama.chat(tools=...)` — neither needs to
    know the call was mediated; both just see a normal callable.

    `on_step`, if given, fires the moment each call is recorded — this is
    what lets the orchestrator stream tool calls to the terminal live
    (PLAN.md Phase 6) regardless of which runner is driving the loop.

    Returns (wrapped_callables, steps) — `steps` is mutated in place as
    calls happen, so the caller can inspect it even mid-loop.
    """
    steps: list[ToolCallStep] = []

    def make_wrapper(name: str, fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(**kwargs):
            start = time.monotonic()
            ok, result, note = budget.invoke(tools, name, kwargs)
            ms = round((time.monotonic() - start) * 1000)
            result_str = _stringify(result) if ok else ""
            step = ToolCallStep(tool_name=name, args=kwargs, result=result_str, note=note, ms=ms)
            steps.append(step)
            if on_step:
                on_step(step)
            if not ok:
                return note
            return result

        return wrapper

    wrapped = [make_wrapper(name, fn) for name, fn in tools.items()]
    return wrapped, steps
