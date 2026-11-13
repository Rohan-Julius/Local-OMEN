"""Evidence validation for the Adjudicator's `confirmed` verdict (audit B6).
Pure Python, no model: every decision here is reproducible from the
verdict, the chunk, the investigation transcript and the files on disk.

Before Phase 3 a `confirmed` verdict survived if `evidence_lines` was
merely non-empty, so the model could cite anything, including lines it
never read. On CPU the audit observed `confirmed` with chunk-line
citations after zero successful tool calls.

A `confirmed` verdict now keeps that status only if it has at least one
evidence line and every line

  (a) parses as `path:line` or `path:a-b` (trailing commentary after the
      location is allowed: `app.py:3: no invalidation call`),
  (b) names an existing file inside the repo root and lines that exist in
      it, and
  (c) lies inside the chunk's own span, or inside lines the Investigator
      actually read according to the transcript: the clamped range of a
      successful `read_code` call, or the location of a `grep_symbol` hit.

Refused calls (a cap, a duplicate), calls that raised and calls whose
result is an error message do not count as reads.

**Chunk-only citations.** If the Investigator made no successful tool call
at all, the Adjudicator saw nothing the chunk did not already show, and
`confirmed` would rest on the chunk alone, which is what Triage already
judged. By default that does not confirm: the verdict is downgraded to
`unverified` even when every citation points into the chunk.
`allow_chunk_only=True` turns this rule off; nothing in Omen sets it.

Anything else is downgraded to `unverified`, and the reason is returned
so the caller can record it (findings.downgrade_reason).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from omen.contracts import AdjudicatorVerdict, Chunk, ToolCallStep, Transcript
from omen.tools import PathEscapesRoot, confine

# path:line or path:a-b, optionally wrapped in backticks, optionally
# followed by commentary. The path may not contain a colon, so a Windows
# drive letter ("C:\...") is not a repo-relative path and fails to parse.
_CITATION = re.compile(r"^\s*`?(?P<path>[^:`\s][^:`]*?)`?:(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?(?!\d)")

READ_TOOLS = ("read_code", "grep_symbol")


def _successful(step: ToolCallStep) -> bool:
    """The tool ran and returned a real result: not refused by a cap, not a
    duplicate served from cache, not an exception, not an error message."""
    if step.note is not None:
        return False
    result = (step.result or "").lstrip()
    return not result.lower().startswith(("error", "(nothing to show"))


def _file_lines(path: Path) -> int | None:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return None


def _resolve(repo_root: Path, rel: str) -> Path | None:
    try:
        resolved = confine(repo_root, rel.strip())
    except PathEscapesRoot:
        return None
    return resolved if resolved.is_file() else None


def read_ranges(transcript: Transcript, repo_root: Path) -> dict[Path, list[tuple[int, int]]]:
    """Lines the Investigator actually read, per resolved file path."""
    ranges: dict[Path, list[tuple[int, int]]] = {}
    for step in transcript.steps:
        if step.tool_name not in READ_TOOLS or not _successful(step):
            continue
        if step.tool_name == "read_code":
            path = _resolve(repo_root, str(step.args.get("file_path", "")))
            n_lines = _file_lines(path) if path else None
            try:
                start, end = int(step.args.get("start_line")), int(step.args.get("end_line"))
            except (TypeError, ValueError):
                continue
            if path is None or n_lines is None:
                continue
            start, end = max(1, start), min(n_lines, end)  # read_code clamps the same way
            if start <= end:
                ranges.setdefault(path, []).append((start, end))
        else:  # grep_symbol: each hit is "path:line: text"
            try:
                hits = json.loads(step.result)
            except (TypeError, ValueError):
                continue
            for hit in hits if isinstance(hits, list) else []:
                m = _CITATION.match(str(hit))
                path = _resolve(repo_root, m.group("path")) if m else None
                if path is not None:
                    line = int(m.group("start"))
                    ranges.setdefault(path, []).append((line, line))
    return ranges


def _covered(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return all(any(a <= line <= b for a, b in spans) for line in range(start, end + 1))


def check_citation(
    line: str,
    repo_root: Path,
    chunk: Chunk,
    ranges: dict[Path, list[tuple[int, int]]],
) -> str | None:
    """None if the citation is valid, otherwise the reason it is not."""
    m = _CITATION.match(line)
    if not m:
        return f"evidence line {line!r} is not a path:line citation"
    rel = m.group("path")
    start = int(m.group("start"))
    end = int(m.group("end") or start)
    if start < 1 or end < start:
        return f"evidence line {line!r} has an invalid line range"
    path = _resolve(repo_root, rel)
    if path is None:
        return f"evidence line {line!r} names a file that does not exist in the repo"
    n_lines = _file_lines(path)
    if n_lines is None or end > n_lines:
        return f"evidence line {line!r} cites lines past the end of the file ({n_lines or 0} lines)"
    spans = list(ranges.get(path, []))
    chunk_path = _resolve(repo_root, chunk.file_path)
    if chunk_path == path:
        spans.append((chunk.start_line, chunk.end_line))
    if not _covered(start, end, spans):
        return f"evidence line {line!r} is outside the chunk and outside every range the Investigator read"
    return None


def validate_confirmed(
    verdict: AdjudicatorVerdict,
    chunk: Chunk,
    transcript: Transcript,
    repo_root: Path,
    allow_chunk_only: bool = False,
) -> tuple[AdjudicatorVerdict, str | None]:
    """Return (final verdict, downgrade reason). Only `confirmed` is ever
    changed, and only ever to `unverified`."""
    if verdict.verdict != "confirmed":
        return verdict, None

    reason = None
    if not verdict.evidence_lines:
        reason = "confirmed with no evidence_lines"
    elif not allow_chunk_only and not any(_successful(s) for s in transcript.steps):
        reason = "the Investigator made no successful tool call, so chunk-line citations alone cannot confirm"
    else:
        ranges = read_ranges(transcript, Path(repo_root))
        for line in verdict.evidence_lines:
            reason = check_citation(line, Path(repo_root), chunk, ranges)
            if reason:
                break

    if reason is None:
        return verdict, None
    return verdict.model_copy(update={"verdict": "unverified"}), reason
