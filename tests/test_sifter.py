"""Phase 8b acceptance (PLAN.md): "on the demo repo's history, the
Sifter reduces N commits to <=3 candidates" and "must reject the decoy
commits (version bumps, typo fixes, test additions)." No LLM involved —
the Sifter is a pure, free, no-inference prefilter by design.
"""
import subprocess

from omen import sifter


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo, path: str, content: str, message: str) -> str:
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", message)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_select_candidates_reduces_and_rejects_decoys(tmp_path):
    _init_repo(tmp_path)

    decoy_shas = [
        _commit(tmp_path, "app.py", "def f():\n    return 1\n", "initial scaffold"),
        _commit(tmp_path, "README.md", "# Project\n", "docs: correct wording in README"),
        _commit(tmp_path, "requirements.txt", "flask==2.0\n", "chore: bump dependency version"),
        _commit(tmp_path, "tests/test_parser.py", "def test_x(): pass\n", "test: add coverage for parser"),
    ]

    real_sha = _commit(
        tmp_path,
        "permissions.py",
        "def revoke(user):\n    cache.delete(user)\n",
        "fix: invalidate permission cache on revoke",
    )
    revert_sha = _commit(
        tmp_path,
        "cache.py",
        "def get(key):\n    return store[key]\n",
        'Revert "add broken caching"\n\nThis reverts commit deadbeef.',
    )
    path_sha = _commit(
        tmp_path,
        "auth/session.py",
        "def start_session(user):\n    pass\n",
        "add session timeout handling",
    )

    candidates = sifter.select_candidates(tmp_path, "HEAD", max_commits=50)
    selected_shas = {c.sha for c in candidates}

    assert len(candidates) <= 3, f"expected the prefilter to reduce to <=3 candidates, got {candidates}"
    assert real_sha in selected_shas
    assert revert_sha in selected_shas
    assert path_sha in selected_shas
    for decoy in decoy_shas:
        assert decoy not in selected_shas, f"decoy commit {decoy[:8]} was not rejected"

    # Oldest-first, so a later Archivist run can dedup a follow-up commit
    # against the incident an earlier one already formed.
    assert [c.sha for c in candidates] == [real_sha, revert_sha, path_sha]

    reasons = {c.sha: c.reason for c in candidates}
    assert reasons[real_sha] == "message:fix"
    assert reasons[revert_sha] == "revert"
    assert reasons[path_sha].startswith("path:")


def test_select_candidates_respects_max_commits(tmp_path):
    _init_repo(tmp_path)
    shas = [_commit(tmp_path, "a.py", f"x = {i}\n", f"fix: iteration {i}") for i in range(5)]

    candidates = sifter.select_candidates(tmp_path, "HEAD", max_commits=2)

    # --max-commits bounds the prefilter's *input* (how many commits it even
    # looks at), not how many pass — with every commit message matching
    # "fix", only the newest 2 should have been considered at all.
    considered_shas = {c.sha for c in candidates}
    assert considered_shas
    assert considered_shas <= set(shas[-2:])


def test_select_candidates_ignores_unrelated_history(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "a.py", "x = 1\n", "add a")
    _commit(tmp_path, "b.py", "y = 2\n", "add b")
    _commit(tmp_path, "README.md", "# hi\n", "docs: initial notes")

    candidates = sifter.select_candidates(tmp_path, "HEAD", max_commits=50)
    assert candidates == []
