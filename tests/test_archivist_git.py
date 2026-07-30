"""Phase 8b acceptance (PLAN.md), the git input path:
- "the Archivist forms a memory whose failure_mechanism describes the
  missing invalidation, not the added invalidation" — the single most
  important check in this phase, per PLAN.md's own "Verification" section:
  "Given a commit that adds an invalidation call, assert the resulting
  failure_mechanism describes the missing invalidation, not the added
  one. A memory that records the fix instead of the failure is worse than
  no memory, and it will read as plausible unless specifically checked."
- "source records the SHA"
- "a second run produces no duplicate"

Live Ollama required (skips gracefully without it).
"""
import subprocess
import urllib.request

import pytest

from omen import archivist, config, runners, store, vectors

BEFORE_CODE = (
    "import functools\n\n"
    "@functools.lru_cache(maxsize=1024)\n"
    "def has_access(user_id, resource_id):\n"
    "    return _compute_permission(user_id, resource_id)\n\n\n"
    "def revoke(user_id, resource_id):\n"
    "    _revoke_in_db(user_id, resource_id)\n"
)

AFTER_CODE = (
    "import functools\n\n"
    "@functools.lru_cache(maxsize=1024)\n"
    "def has_access(user_id, resource_id):\n"
    "    return _compute_permission(user_id, resource_id)\n\n\n"
    "def revoke(user_id, resource_id):\n"
    "    _revoke_in_db(user_id, resource_id)\n"
    "    has_access.cache_clear()\n"
)

FIX_COMMIT_MESSAGE = (
    "fix: clear permission cache when access is revoked\n\n"
    "Revoked users could still pass has_access() checks until the "
    "lru_cache entry expired on its own, because nothing cleared it when "
    "revoke() ran. Call cache_clear() on revoke so it takes effect "
    "immediately."
)


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")


def _rev_parse_head(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _build_fix_commit(repo) -> str:
    """A repo with one commit lacking cache invalidation on revoke, then a
    second (the one under test) that adds it — the classic "fix commit
    shows the fix, not the failure" trap PLAN.md calls out."""
    _init_repo(repo)
    (repo / "permissions.py").write_text(BEFORE_CODE)
    _git(repo, "add", "permissions.py")
    _git(repo, "commit", "-qm", "add permission check with in-memory cache")

    (repo / "permissions.py").write_text(AFTER_CODE)
    _git(repo, "add", "permissions.py")
    _git(repo, "commit", "-qm", FIX_COMMIT_MESSAGE)
    return _rev_parse_head(repo)


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")


@pytest.fixture(autouse=True)
def isolated_chroma(tmp_path, monkeypatch):
    # Same hazard as tests/test_archivist.py: write_incident's reindex()
    # rewrites the real on-disk Chroma collection unless redirected.
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")


def _has_negation_near_invalidation(mechanism: str) -> bool:
    """Loose proxy for "describes an absence, not a presence" — there is no
    clean way to assert exact wording an LLM will choose, but a correctly
    inverted mechanism sentence must combine a negation word with the
    invalidation/staleness concept somewhere in it."""
    lower = mechanism.lower()
    negation_words = ("no ", "not ", "without", "never", "lack", "missing", "doesn't", "does not", "isn't", "wasn't", "absent")
    concept_words = ("invalidat", "clear", "revoke", "stale", "expire")
    return any(n in lower for n in negation_words) and any(c in lower for c in concept_words)


@pytest.mark.asyncio
async def test_archivist_records_pre_change_failure_not_the_fix(tmp_path):
    repo = tmp_path / "repo"
    sha = _build_fix_commit(repo)

    conn = store.connect(tmp_path / "test.db")
    runner = runners.ADKRoleRunner()

    transcript = await archivist.learn_from_commit(runner, conn, repo, sha, think=False)
    ref = archivist.written_ref(transcript)
    assert ref, f"Archivist never wrote an incident: {transcript.final_text}"

    incident = store.get_incident(conn, ref)
    assert incident is not None
    assert incident.source == f"git:{sha}"
    assert incident.learned_by == "archivist:git"

    mechanism = incident.failure_mechanism
    assert _has_negation_near_invalidation(mechanism), (
        f"failure_mechanism doesn't read as describing an absence — looks like it recorded the "
        f"fix instead of the pre-change failure: {mechanism!r}"
    )
    # The literal fix action shouldn't be the mechanism sentence itself.
    assert "cache_clear()" not in mechanism


@pytest.mark.asyncio
async def test_archivist_git_dedups_on_second_run(tmp_path):
    repo = tmp_path / "repo"
    sha = _build_fix_commit(repo)

    conn = store.connect(tmp_path / "test.db")
    runner = runners.ADKRoleRunner()

    first = await archivist.learn_from_commit(runner, conn, repo, sha, think=False)
    first_ref = archivist.written_ref(first)
    assert first_ref

    second = await archivist.learn_from_commit(runner, conn, repo, sha, think=False)
    second_ref = archivist.written_ref(second)
    assert second_ref, f"second run never wrote/updated an incident: {second.final_text}"

    assert first_ref == second_ref, (
        f"second run created a new ref ({second_ref}) instead of updating the first ({first_ref})"
    )
    assert len(store.list_incidents(conn)) == 1
