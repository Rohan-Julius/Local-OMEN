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


# ---------------------------------------------------------------------------
# Phase 1 regressions (B4, B10), against a fake runner: no model needed.
# ---------------------------------------------------------------------------

import re  # noqa: E402

from omen import runners, tools as tools_mod, vectors  # noqa: E402
from omen.contracts import Transcript  # noqa: E402


class _ScriptedArchivist:
    """Stands in for a RoleRunner. Calls the real (budgeted) learn tools with
    a fixed script, so the CLI's tool wiring is tested, not prompt quality."""

    calls: list[tuple[str, dict]] = []

    async def run_tooled(self, instruction, prompt, tools, budget, think=True, on_step=None, on_text=None):
        wrapped, steps = tools_mod.budgeted_tools(tools, budget, on_step=on_step)
        by_name = {fn.__name__: fn for fn in wrapped}
        prompt_path = re.search(r"path '([^']+)'", prompt).group(1)
        script = [("read_file", {"file_path": prompt_path})] + type(self).calls
        for name, kwargs in script:
            by_name[name](**kwargs)
        return Transcript(steps=steps, final_text="done", stopped_reason="model_finished")


@pytest.fixture
def learn_sandbox(tmp_path, monkeypatch):
    """cwd = a fake project root holding a fake `.env`; the postmortem
    lives in its own subdirectory. The real prompts load from the package,
    so changing the cwd away from the Omen checkout must not break them."""
    project = tmp_path / "project"
    (project / "postmortems").mkdir(parents=True)
    (project / ".env").write_text("FAKE_SECRET=do-not-leak\n")
    (project / "postmortems" / "incident.md").write_text("# Postmortem\nthe cache never invalidated\n")
    monkeypatch.chdir(project)
    monkeypatch.setattr(runners, "ADKRoleRunner", _ScriptedArchivist)
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "embed_query", lambda texts: [[1.0] for _ in texts])
    real_connect = store_mod.connect
    monkeypatch.setattr(store_mod, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))
    return project


@pytest.mark.asyncio
async def test_b4_learn_postmortem_read_file_is_confined_to_the_postmortem_dir(learn_sandbox, monkeypatch, capsys):
    """B4: the Archivist's read_file used to be rooted at the cwd, so a
    model (or a prompt-injected postmortem) could read the project `.env`."""
    monkeypatch.setattr(
        _ScriptedArchivist, "calls", [("read_file", {"file_path": "../.env"}), ("read_file", {"file_path": ".env"})]
    )
    await cli.cmd_learn(_base_args(postmortem_path="postmortems/incident.md"))

    out = capsys.readouterr().out
    assert "the cache never invalidated" in out  # the postmortem itself was readable
    assert "FAKE_SECRET" not in out


@pytest.mark.asyncio
async def test_b4_learn_postmortem_outside_allow_dir_is_rejected(learn_sandbox, capsys):
    (learn_sandbox / "docs").mkdir()
    await cli.cmd_learn(_base_args(postmortem_path="postmortems/incident.md", allow_dir="docs"))
    out = capsys.readouterr().out
    assert "error" in out and "--allow-dir" in out


@pytest.mark.asyncio
async def test_b10_failed_write_incident_reports_no_incident_written(learn_sandbox, monkeypatch, capsys):
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_mod, "upsert_incident", locked)
    monkeypatch.setattr(
        _ScriptedArchivist,
        "calls",
        [("write_incident", dict(title="t", failure_mechanism="m", what_happened="h", the_rule="r", surface_forms=["f"]))],
    )
    await cli.cmd_learn(_base_args(postmortem_path="postmortems/incident.md"))

    out = capsys.readouterr().out
    assert "No incident was written." in out
    assert "Learned/updated" not in out
