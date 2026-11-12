"""Phase 8 (production plan), audit B25: scripts/build_demo_repo.py recreates
demo_repo's nested git history so `omen learn --from-git --repo demo_repo`
works from a clone. Everything runs in a temporary directory, on a COPY of
demo_repo/: the real demo_repo (and any .git inside it) is never touched.
No model.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from omen import sifter

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo_repo"

_spec = importlib.util.spec_from_file_location("build_demo_repo", ROOT / "scripts" / "build_demo_repo.py")
build_demo_repo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_demo_repo)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout


@pytest.fixture
def demo_copy(tmp_path) -> Path:
    """The demo_repo files as a fresh clone has them: no .git."""
    dest = tmp_path / "demo_repo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    return dest


def snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file() and ".git" not in p.parts}


def test_builds_seven_commits_and_leaves_the_working_tree_unchanged(demo_copy):
    before = snapshot(demo_copy)

    made = build_demo_repo.build(demo_copy)

    assert [subject for _, subject in made] == [
        "scaffold admin API demo service",
        "bump flask to 2.3.3",
        "add admin permission checks and a status page",
        "add a test for the request window",
        "tidy the models docstring",
        "fix: ignore X-Forwarded-For from clients that are not trusted proxies",
        "bump requests to 2.30.0",
    ]
    assert git(demo_copy, "rev-list", "--count", "HEAD").strip() == "7"
    assert snapshot(demo_copy) == before  # byte for byte what was there
    assert git(demo_copy, "status", "--porcelain").strip() == ""  # and committed: no uncommitted diff
    assert git(demo_copy, "symbolic-ref", "--short", "HEAD").strip() == "main"


def test_the_sifter_selects_the_fix_and_rejects_the_decoys(demo_copy):
    build_demo_repo.build(demo_copy)

    candidates = sifter.select_candidates(demo_copy, "HEAD", max_commits=50)

    assert [c.subject for c in candidates] == [
        "add admin permission checks and a status page",
        "fix: ignore X-Forwarded-For from clients that are not trusted proxies",
    ]
    assert candidates[0].reason.startswith("path:") and candidates[1].reason == "message:fix"  # oldest first


def test_the_fix_commit_shows_the_failure_in_its_pre_change_state(demo_copy):
    build_demo_repo.build(demo_copy)
    fix = next(c for c in sifter.select_candidates(demo_copy, "HEAD", 50) if c.reason == "message:fix")

    before = git(demo_copy, "show", f"{fix.sha}~1:ratelimit.py")
    after = git(demo_copy, "show", f"{fix.sha}:ratelimit.py")

    assert 'if "X-Forwarded-For" in headers:' in before and "_TRUSTED_PROXIES" not in before  # any client's header was trusted
    assert 'remote_addr in _TRUSTED_PROXIES and "X-Forwarded-For" in headers' in after
    assert "bypassed the limit" in git(demo_copy, "log", "-1", "--format=%B", fix.sha)


def test_decoys_touch_nothing_the_sifter_looks_for(demo_copy):
    build_demo_repo.build(demo_copy)
    selected = {c.sha for c in sifter.select_candidates(demo_copy, "HEAD", 50)}
    for sha, subject, *_ in (line.split("\x1f") for line in git(demo_copy, "log", "--format=%H%x1f%s").splitlines()):
        if sha not in selected:
            assert not sifter.MESSAGE_PATTERN.search(subject), subject
            names = git(demo_copy, "show", "--name-only", "--format=", sha).split()
            assert not any(sifter.SENSITIVE_PATH_PATTERN.search(n) for n in names), (subject, names)


def test_an_existing_dot_git_is_never_touched(demo_copy):
    marker = demo_copy / ".git"
    marker.mkdir()
    (marker / "MARKER").write_text("do not touch", encoding="utf-8")
    before = snapshot(demo_copy)

    assert build_demo_repo.build(demo_copy) == []
    assert build_demo_repo.main(["--dest", str(demo_copy)]) == 0

    assert [p.name for p in marker.iterdir()] == ["MARKER"] and (marker / "MARKER").read_text(encoding="utf-8") == "do not touch"
    assert snapshot(demo_copy) == before


def test_running_it_twice_is_harmless(demo_copy, capsys):
    assert build_demo_repo.main(["--dest", str(demo_copy)]) == 0
    head = git(demo_copy, "rev-parse", "HEAD").strip()
    assert "built 7 commit(s)" in capsys.readouterr().out

    assert build_demo_repo.main(["--dest", str(demo_copy)]) == 0

    assert git(demo_copy, "rev-parse", "HEAD").strip() == head and "left untouched" in capsys.readouterr().out


def test_two_builds_give_the_same_commit_ids(demo_copy, tmp_path):
    other = tmp_path / "second"
    shutil.copytree(demo_copy, other)
    assert build_demo_repo.build(demo_copy) == build_demo_repo.build(other)


def test_a_folder_that_is_not_demo_repo_is_refused(tmp_path, capsys):
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    assert build_demo_repo.main(["--dest", str(tmp_path)]) == 1
    assert "missing" in capsys.readouterr().err and not (tmp_path / ".git").exists()
    assert build_demo_repo.main(["--dest", str(tmp_path / "nope")]) == 1


def test_a_changed_demo_file_is_reported_not_silently_mangled(demo_copy, capsys):
    path = demo_copy / "ratelimit.py"
    path.write_text(path.read_text(encoding="utf-8").replace("_TRUSTED_PROXIES", "_PROXIES"), encoding="utf-8")
    before = snapshot(demo_copy)

    assert build_demo_repo.main(["--dest", str(demo_copy)]) == 1

    assert "demo_repo has changed" in capsys.readouterr().err
    assert snapshot(demo_copy) == before  # the working tree was restored


def test_building_inside_another_repository_never_commits_to_the_outer_one(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q")
    git(outer, "config", "user.email", "t@t")
    git(outer, "config", "user.name", "T")
    (outer / "README.md").write_text("outer\n", encoding="utf-8")
    git(outer, "add", "README.md")
    git(outer, "commit", "-qm", "outer commit")
    inner = outer / "demo_repo"  # a clone: the demo files live inside the outer repository
    shutil.copytree(DEMO, inner, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    outer_head = git(outer, "rev-parse", "HEAD").strip()

    build_demo_repo.build(inner)

    assert git(outer, "rev-parse", "HEAD").strip() == outer_head
    assert git(outer, "rev-list", "--count", "HEAD").strip() == "1"
    assert git(inner, "rev-parse", "--show-toplevel").strip().replace("\\", "/").endswith("outer/demo_repo")
    assert git(inner, "rev-list", "--count", "HEAD").strip() == "7"


def test_the_real_demo_repo_is_what_the_script_expects():
    """If someone edits demo_repo, this fails here instead of at a user's first run."""
    for rel in build_demo_repo.EXPECTED_FILES:
        assert (DEMO / rel).is_file(), rel
    for _message, _date, files in build_demo_repo.HISTORY:
        for path in files:
            assert path in build_demo_repo.EXPECTED_FILES
