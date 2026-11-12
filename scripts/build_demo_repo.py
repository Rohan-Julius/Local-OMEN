#!/usr/bin/env python
"""Recreate demo_repo's nested git history (audit B25).

`demo_repo/` is a small example service. Its files are tracked by the outer
Omen repository, but its own `.git/` is deliberately ignored (`.gitignore`),
so a fresh clone has the files and no history, and `omen learn --from-git
--repo demo_repo` would read Omen's own history instead. This script gives
the clone the history `learn --from-git` is meant to be tried on:

    python scripts/build_demo_repo.py            # builds demo_repo/.git
    omen learn --from-git HEAD --repo demo_repo --dry-run

It is a RECONSTRUCTION, not the original history (which was never pushed):
seven commits, built from the files already in the folder, ending with a
working tree identical to what is on disk (no uncommitted diff).

    1. scaffold admin API demo service           decoy
    2. bump flask to 2.3.3                       decoy
    3. add admin permission checks and a cache   path signal (permission, cache)
    4. add a test for the request window         decoy
    5. tidy the models docstring                 decoy
    6. fix: ignore X-Forwarded-For ...           the real fix: message:fix
    7. bump requests to 2.30.0                   decoy

Run against that history, the Sifter selects exactly two commits (3 by path,
6 by message) and rejects the five decoys, whose messages avoid the
prefilter's keywords and whose paths avoid auth/cache/payment/permission.
The fix commit's pre-change state is the failure: ratelimit.py trusted a
client-supplied X-Forwarded-For header from anyone, so a client could pick its
own rate-limit bucket.

Safety: an existing `demo_repo/.git` is never touched (the script says so and
exits 0). Commits are made only after checking that the new repository really
is the one inside the target folder, never the outer one. Authors and dates
are fixed so two runs give the same commit ids (on one machine).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "demo_repo"

AUTHOR = ("Omen Demo", "demo@omen.invalid")
EXPECTED_FILES = [
    "app.py",
    "cache/response_cache.py",
    "models.py",
    "permissions.py",
    "ratelimit.py",
    "requirements.txt",
    "tests/test_ratelimit.py",
]


class BuildError(Exception):
    """A problem the user can act on (missing files, a repo in the wrong place)."""


# ---------------------------------------------------------------------------
# earlier versions of the files, derived from the files as they are now
# ---------------------------------------------------------------------------


def _swap(text: str, now: str, before: str) -> str:
    if now not in text:
        raise BuildError(f"demo_repo has changed: expected to find {now!r}; update scripts/build_demo_repo.py")
    return text.replace(now, before, 1)


def _requirements(flask: str, requests: str) -> Callable[[str], str]:
    return lambda t: _swap(_swap(t, "flask==2.3.3", f"flask=={flask}"), "requests==2.30.0", f"requests=={requests}")


def _ratelimit_before_fix(t: str) -> str:
    t = _swap(t, '_TRUSTED_PROXIES = {"10.0.0.1", "10.0.0.2"}\n', "")
    return _swap(t, 'if remote_addr in _TRUSTED_PROXIES and "X-Forwarded-For" in headers:', 'if "X-Forwarded-For" in headers:')


def _tests_first_only(t: str) -> str:
    cut = t.find("def test_blocks_requests_once_over_the_limit")
    if cut < 0:
        raise BuildError("demo_repo has changed: tests/test_ratelimit.py has no test_blocks_requests_once_over_the_limit")
    return t[:cut].rstrip("\n") + "\n"


def _tests_without_xff(t: str) -> str:
    cut = t.find("def test_ignores_forwarded_for_from_an_untrusted_client")
    if cut < 0:
        raise BuildError("demo_repo has changed: tests/test_ratelimit.py has no test_ignores_forwarded_for_from_an_untrusted_client")
    return t[:cut].rstrip("\n") + "\n"


def _models_before_tidy(t: str) -> str:
    return _swap(t, '"""Plain data models for the internal admin API."""', '"""Data models."""')


def identity(text: str) -> str:
    return text


# (message, date, {path: transform of the CURRENT text into the text at that commit})
# A path not listed keeps its previous content; "identity" means "the file as it is now".
FIX_MESSAGE = (
    "fix: ignore X-Forwarded-For from clients that are not trusted proxies\n\n"
    "Any client could send an X-Forwarded-For header and pick its own rate-limit\n"
    "bucket, so rotating the header value bypassed the limit entirely. Only\n"
    "honour the header when the connection comes from a trusted proxy address."
)

HISTORY: list[tuple[str, str, dict[str, Callable[[str], str]]]] = [
    (
        "scaffold admin API demo service",
        "2025-08-01T09:00:00",
        {
            "app.py": identity,
            "models.py": _models_before_tidy,
            "ratelimit.py": _ratelimit_before_fix,
            "requirements.txt": _requirements("2.3.2", "2.28.2"),
            "tests/test_ratelimit.py": _tests_first_only,
        },
    ),
    ("bump flask to 2.3.3", "2025-08-02T09:00:00", {"requirements.txt": _requirements("2.3.3", "2.28.2")}),
    (
        "add admin permission checks and a status page",
        "2025-08-04T09:00:00",
        {"permissions.py": identity, "cache/response_cache.py": identity},
    ),
    (
        "add a test for the request window",
        "2025-08-05T09:00:00",
        {"tests/test_ratelimit.py": _tests_without_xff},
    ),
    ("tidy the models docstring", "2025-08-06T09:00:00", {"models.py": identity}),
    (
        FIX_MESSAGE,
        "2025-08-07T09:00:00",
        {"ratelimit.py": identity, "tests/test_ratelimit.py": identity},
    ),
    ("bump requests to 2.30.0", "2025-08-08T09:00:00", {"requirements.txt": identity}),
]


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _env(date: str | None = None) -> dict[str, str]:
    """The environment for git: nothing inherited that could point it at another repository."""
    env = {k: v for k, v in os.environ.items() if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}}
    if date:
        env.update(GIT_AUTHOR_DATE=f"{date} +0000", GIT_COMMITTER_DATE=f"{date} +0000")
    return env


def _git(dest: Path, *args: str, date: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}", "-c", "commit.gpgsign=false", "-C", str(dest), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(date),
    )
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _read(path: Path) -> tuple[str, str]:
    """(text normalised to LF, the file's own line ending)."""
    raw = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), eol


def build(dest: Path) -> list[tuple[str, str]]:
    """Build `dest/.git` and return [(short sha, subject)] oldest first. An existing
    `dest/.git` is left alone and an empty list is returned."""
    dest = Path(dest).resolve()
    if not dest.is_dir():
        raise BuildError(f"{dest} is not a directory")
    if (dest / ".git").exists():
        return []
    missing = [p for p in EXPECTED_FILES if not (dest / p).is_file()]
    if missing:
        raise BuildError(f"{dest} is missing {', '.join(missing)}; is this the demo_repo folder?")

    finals: dict[str, tuple[str, str]] = {p: _read(dest / p) for p in EXPECTED_FILES}
    original_bytes = {p: (dest / p).read_bytes() for p in EXPECTED_FILES}

    _git(dest, "init", "-q")
    _git(dest, "symbolic-ref", "HEAD", "refs/heads/main")
    top = Path(_git(dest, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != dest:  # git picked another repository: stop before the first commit
        raise BuildError(f"git is using the repository at {top}, not {dest}; nothing was committed")

    made: list[tuple[str, str]] = []
    try:
        for message, date, files in HISTORY:
            for path, transform in files.items():
                text, eol = finals[path]
                is_final = transform is identity
                data = original_bytes[path] if is_final else transform(text).replace("\n", eol).encode("utf-8")
                (dest / path).write_bytes(data)
                _git(dest, "add", "--", path)
            _git(dest, "commit", "-q", "-m", message, date=date)
            sha = _git(dest, "rev-parse", "--short", "HEAD").strip()
            made.append((sha, message.splitlines()[0]))
    finally:
        # The working tree must end exactly as it started, even if a step failed.
        for path, data in original_bytes.items():
            (dest / path).write_bytes(data)
    status = _git(dest, "status", "--porcelain").strip()
    if status:
        raise BuildError(f"the working tree is not clean after building the history:\n{status}")
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recreate demo_repo's nested git history (so `omen learn --from-git --repo demo_repo` works from a clone).")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="The demo_repo folder (default: demo_repo/ next to this script's parent)")
    args = parser.parse_args(argv)
    dest = Path(args.dest)
    try:
        made = build(dest)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not made:
        print(f"{dest / '.git'} already exists: left untouched.")
        return 0
    print(f"built {len(made)} commit(s) in {dest}:")
    for sha, subject in made:
        print(f"  {sha}  {subject}")
    print(f"\nTry: omen learn --from-git HEAD --repo {args.dest} --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
