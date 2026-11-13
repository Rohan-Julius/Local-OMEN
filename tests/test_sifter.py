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


# ---------------------------------------------------------------------------
# B30: git output must be read as UTF-8 and paths unquoted (like Scout, B29)
# ---------------------------------------------------------------------------

# "Ł" is UTF-8 C5 81 and "Ý" is C3 9D: 0x81 and 0x9D are undefined in cp1252,
# so decoding this subject with the Windows locale encoding fails outright.
UTF8_SUBJECT = "fix: Łódź café cache returned Ýears-old data"


def test_b30_commit_subject_with_utf8_bytes_is_read_exactly(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "README.md", "hello\n", "initial commit")
    sha = _commit(tmp_path, "notes.txt", "x\n", UTF8_SUBJECT)

    candidates = sifter.select_candidates(tmp_path, "HEAD~1..HEAD", max_commits=50)

    assert [(c.sha, c.subject, c.reason) for c in candidates] == [(sha, UTF8_SUBJECT, "message:fix")]


def test_b30_non_ascii_path_in_a_sensitive_directory(tmp_path):
    """The path-signal commit must be selected, and the Sifter must see the
    real path, not git's quoted form (`"na\303\257ve_auth/..."`)."""
    _init_repo(tmp_path)
    _commit(tmp_path, "README.md", "hello\n", "initial commit")
    sha = _commit(tmp_path, "naïve_auth/permissões.py", "ALLOW = True\n", "tidy up module layout")

    candidates = sifter.select_candidates(tmp_path, "HEAD~1..HEAD", max_commits=50)

    assert [(c.sha, c.reason) for c in candidates] == [(sha, "path:auth")]
    assert sifter._changed_paths(tmp_path, sha) == ["naïve_auth/permissões.py"]
