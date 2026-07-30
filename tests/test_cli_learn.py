"""CLI-level acceptance for `omen learn`:
- exactly one of a postmortem path or --from-git must be given (Phase 8b
  extended the Phase 8a command to accept either source).
- PLAN.md Phase 8b: "--dry-run writes nothing" — verified here at the CLI
  dispatch level, not just inside sifter.py, since `--dry-run` is a CLI
  concern (it short-circuits before any Archivist/tool call happens).

No live Ollama needed: --dry-run never reaches the model, and the
validation-error paths return before constructing anything that would.
"""
import argparse
import subprocess

import pytest

from omen import cli, store as store_mod


def _base_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        postmortem_path=None,
        from_git=None,
        repo=".",
        max_commits=50,
        max_learn=5,
        dry_run=False,
        runner="adk",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.mark.asyncio
async def test_cmd_learn_rejects_neither_source(capsys):
    await cli.cmd_learn(_base_args())
    assert "exactly one" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cmd_learn_rejects_both_sources(capsys):
    await cli.cmd_learn(_base_args(postmortem_path="postmortems/x.md", from_git="HEAD~5..HEAD"))
    assert "exactly one" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cmd_learn_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@test.com")
    git("config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("commit", "-qm", "fix: something a future scan should remember")

    real_connect = store_mod.connect
    monkeypatch.setattr(store_mod, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))

    await cli.cmd_learn(_base_args(from_git="HEAD", repo=str(repo), dry_run=True))

    out = capsys.readouterr().out
    assert "candidate" in out
    assert "no commits will be learned" in out.lower()

    conn = real_connect(tmp_path / "test.db")
    assert store_mod.list_incidents(conn) == []
