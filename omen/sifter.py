"""Phase 8b: the deterministic prefilter for `omen learn --from-git`. Pure
Python, no inference — PLAN.md: "Deterministic prefilter (free, no
inference)... Zero cost, and on a real repo it removes the large majority."
An optional LLM triage second stage is deliberately not built: "Build this
only if the deterministic prefilter proves too noisy in practice. Start
without it."

The Archivist is expensive (~30-45s per invocation with a tool loop), so
this module's whole job is to keep it from ever running on a commit that
obviously isn't a remembered failure.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# PLAN.md 8b: "Commit message patterns — fix, hotfix, revert, regression,
# incident, postmortem, rollback, CVE — plus structural signals: a commit
# that reverts another commit, or a diff touching auth / cache / payment /
# permission paths."
MESSAGE_PATTERN = re.compile(r"\b(fix|hotfix|revert|regression|incident|postmortem|rollback|cve)\b", re.IGNORECASE)
SENSITIVE_PATH_PATTERN = re.compile(r"auth|cache|payment|permission", re.IGNORECASE)

# Between commit fields in `git log --format`, using bytes that will never
# appear in a commit message (ASCII unit/record separators).
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


@dataclass
class CommitCandidate:
    sha: str
    subject: str
    reason: str  # human-readable: why the prefilter selected this commit


def _run_git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, text=True)


def _log_range(repo_path: Path, commit_range: str, max_commits: int) -> list[tuple[str, str, str]]:
    """(sha, subject, body) for up to `max_commits` commits in `commit_range`,
    oldest first — so later processing (the Archivist, per-commit) can dedup
    a follow-up commit against the incident an earlier one already formed
    (PLAN.md: "Chroma must be reindexed after each write rather than once
    at the end... otherwise the 40th commit cannot see the memory formed
    from the 12th")."""
    result = _run_git(
        repo_path,
        ["log", "--reverse", f"--format=%H{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}", f"-{max_commits}", commit_range],
    )
    entries = [e for e in result.stdout.split(_RECORD_SEP) if e.strip()]
    commits = []
    for entry in entries:
        parts = entry.strip("\n").split(_FIELD_SEP)
        if len(parts) < 2:
            continue
        sha, subject = parts[0].strip(), parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        commits.append((sha, subject, body))
    return commits


def _changed_paths(repo_path: Path, sha: str) -> list[str]:
    result = _run_git(repo_path, ["show", "--name-only", "--format=", sha])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _select_reason(subject: str, body: str, paths: list[str]) -> str | None:
    """None means "not a candidate". A commit is selected by the first
    signal that matches, cheapest/most-specific first."""
    if subject.lower().startswith("revert") or "this reverts commit" in body.lower():
        return "revert"
    m = MESSAGE_PATTERN.search(subject)
    if m:
        return f"message:{m.group(1).lower()}"
    for path in paths:
        m = SENSITIVE_PATH_PATTERN.search(path)
        if m:
            return f"path:{m.group(0).lower()}"
    return None


def select_candidates(repo_path: Path, commit_range: str, max_commits: int) -> list[CommitCandidate]:
    """Reduce `commit_range` to the commits that plausibly encode a
    remembered failure. Returns candidates oldest-first (see `_log_range`).
    `max_commits` bounds how many commits from the range are even
    considered (PLAN.md: "--max-commits (default 50) bounds the prefilter
    input"), not how many candidates come out the other end."""
    commits = _log_range(repo_path, commit_range, max_commits)
    candidates = []
    for sha, subject, body in commits:
        paths = _changed_paths(repo_path, sha)
        reason = _select_reason(subject, body, paths)
        if reason:
            candidates.append(CommitCandidate(sha=sha, subject=subject, reason=reason))
    return candidates
