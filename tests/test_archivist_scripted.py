"""Phase 5 (production plan): the Archivist, end to end, with a SCRIPTED fake
runner (no model). Real tools, real ToolBudget, real SQLite and Chroma
(fake embedder), driven through `archivist.learn_from_postmortem` and
`cli.cmd_learn`.

This is the coverage the four live Archivist tests cannot give CI. It also
pins the summary rule: after a refused or failed write the model's own text
is NOT shown (in the Phase 4 diagnostic it claimed "recorded ... OMEN-00x"
after its write was refused); `Learned/updated <ref>` comes only from the
written ref.
"""
from __future__ import annotations

import argparse

import pytest

from omen import archivist, cli, runners, store, tools as tools_mod, vectors
from omen.contracts import IncidentSeed, Transcript
from tests.test_memory_integrity import CURATED_FORMS, fake_embed, seed_incident

CLAIM = "I have recorded the incident under the mechanism of shared global state. The new reference is OMEN-001."
NEW_INCIDENT = dict(
    title="Request state in a module global",
    failure_mechanism="Per-request state is kept in a module-level variable shared by concurrent requests.",
    what_happened="One user's data was returned to another.",
    the_rule="Keep request state in request-scoped storage.",
    surface_forms=["a global CURRENT_USER set by middleware", "a module-level cache of the logged-in user"],
)


class ScriptedArchivist:
    """Calls the real (budgeted) learn tools in order, then returns `final_text`.
    `script` items are (tool name, kwargs) or the string "deadline" (make the
    budget refuse everything after this point, as an expired deadline does)."""

    def __init__(self, script, final_text=CLAIM):
        self.script, self.final_text = script, final_text

    async def run_tooled(self, instruction, prompt, tools, budget, think=True, on_step=None, on_text=None):
        wrapped, steps = tools_mod.budgeted_tools(tools, budget, on_step=on_step)
        by_name = {fn.__name__: fn for fn in wrapped}
        for item in self.script:
            if item == "deadline":
                budget.wall_clock_seconds = -1
                continue
            name, kwargs = item
            by_name[name](**kwargs)
        from omen.runners import _stopped_reason

        return Transcript(steps=steps, final_text=self.final_text, stopped_reason=_stopped_reason(budget, steps, self.final_text))


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "postmortems").mkdir(parents=True)
    (project / "postmortems" / "pm.md").write_text("# Postmortem\nrequest state in a global\n")
    monkeypatch.chdir(project)
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "_embed", fake_embed)
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(tmp_path / "t.db"))
    return real_connect(tmp_path / "t.db"), project


def install(monkeypatch, runner):
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: runner)


def learn_args(**kw):
    base = dict(postmortem_path="postmortems/pm.md", from_git=None, repo=".", max_commits=50, max_learn=5,
                dry_run=False, runner="adk", no_warmup=True, profile="gpu", allow_dir=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# through archivist.learn_from_postmortem
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_success_creates_an_incident_and_written_ref_finds_it(env):
    conn, project = env
    runner = ScriptedArchivist([("read_file", {"file_path": "pm.md"}), ("write_incident", NEW_INCIDENT)])

    transcript = await archivist.learn_from_postmortem(runner, conn, project / "postmortems", "pm.md", think=False)

    assert archivist.written_ref(transcript) == "OMEN-001"
    assert transcript.stopped_reason == "model_finished"
    assert store.get_incident(conn, "OMEN-001").title == NEW_INCIDENT["title"]
    assert vectors.count() == 3  # the mechanism + two surface forms, indexed incrementally


@pytest.mark.asyncio
async def test_refused_write_is_not_a_written_ref(env):
    conn, project = env
    runner = ScriptedArchivist([("read_file", {"file_path": "pm.md"}), "deadline", ("write_incident", NEW_INCIDENT)])

    transcript = await archivist.learn_from_postmortem(runner, conn, project / "postmortems", "pm.md", think=False)

    assert archivist.written_ref(transcript) is None  # the model still claims success in final_text
    assert transcript.final_text == CLAIM
    assert transcript.stopped_reason == "wall_clock_cap"
    assert store.list_incidents(conn) == []


@pytest.mark.asyncio
async def test_merge_via_existing_ref_keeps_curated_text(env):
    conn, project = env
    seed_incident(conn, "OMEN-001")
    vectors.reindex(store.list_incidents(conn))
    merge = {**NEW_INCIDENT, "existing_ref": "OMEN-001"}
    runner = ScriptedArchivist([("search_memory", {"query": "request state"}), ("write_incident", merge)])

    transcript = await archivist.learn_from_postmortem(runner, conn, project / "postmortems", "pm.md", think=False)

    assert archivist.written_ref(transcript) == "OMEN-001"
    inc = store.get_incident(conn, "OMEN-001")
    assert inc.title == "Stale permission cache"  # curated, kept
    assert inc.surface_forms == [*CURATED_FORMS, *NEW_INCIDENT["surface_forms"]]  # union, originals first
    assert [s["learned_by"] for s in inc.sources] == ["seed", "archivist:postmortem"]
    assert [r["action"] for r in store.list_history(conn, "OMEN-001")] == ["create", "update"]
    assert vectors.count() == 1 + 6  # mechanism + 4 curated + 2 new, nothing else touched


@pytest.mark.asyncio
async def test_a_failed_write_is_not_a_written_ref(env):
    conn, project = env
    runner = ScriptedArchivist([("write_incident", {**NEW_INCIDENT, "existing_ref": "OMEN-042"})])  # unknown ref: raises
    transcript = await archivist.learn_from_postmortem(runner, conn, project / "postmortems", "pm.md", think=False)
    assert transcript.steps[0].note == "error" and archivist.written_ref(transcript) is None


# ---------------------------------------------------------------------------
# through cli.cmd_learn: what the user is told
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_after_a_real_write_shows_the_ref_and_labels_the_models_text(env, monkeypatch, capsys):
    install(monkeypatch, ScriptedArchivist([("write_incident", NEW_INCIDENT)], final_text="Wrote the incident."))

    assert await cli.cmd_learn(learn_args()) is None

    out = capsys.readouterr().out
    assert "Learned/updated OMEN-001" in out
    assert "[archivist] model's note: Wrote the incident." in out
    assert "summary:" not in out and "No incident was written" not in out


@pytest.mark.asyncio
async def test_summary_after_a_refused_write_does_not_repeat_the_models_claim(env, monkeypatch, capsys):
    install(monkeypatch, ScriptedArchivist([("read_file", {"file_path": "pm.md"}), "deadline", ("write_incident", NEW_INCIDENT)]))

    await cli.cmd_learn(learn_args())

    out = capsys.readouterr().out
    assert "No incident was written (wall_clock_cap)." in out
    assert "Learned/updated" not in out
    assert CLAIM not in out and "I have recorded" not in out and "OMEN-001" not in out  # the claim is not shown
    assert "[archivist] stopped_reason=wall_clock_cap" in out


@pytest.mark.asyncio
async def test_summary_after_a_merge_names_the_merged_ref(env, monkeypatch, capsys):
    conn, _ = env
    seed_incident(conn, "OMEN-001")
    vectors.reindex(store.list_incidents(conn))
    install(monkeypatch, ScriptedArchivist([("write_incident", {**NEW_INCIDENT, "existing_ref": "OMEN-001"})], final_text="Merged."))

    await cli.cmd_learn(learn_args())

    out = capsys.readouterr().out
    assert "Learned/updated OMEN-001" in out and "model's note: Merged." in out


@pytest.mark.asyncio
async def test_an_llm_timeout_is_reported_with_its_note_and_no_success_text(env, monkeypatch, capsys):
    class TimedOut:
        async def run_tooled(self, *a, **k):
            return Transcript(final_text=CLAIM, stopped_reason="llm_timeout", note="an Investigator/Archivist model turn timed out after 600s")

    install(monkeypatch, TimedOut())
    await cli.cmd_learn(learn_args())
    out = capsys.readouterr().out
    assert "stopped_reason=llm_timeout" in out and "[archivist] note: an Investigator/Archivist model turn timed out after 600s" in out
    assert "No incident was written (llm_timeout)." in out and CLAIM not in out


@pytest.mark.asyncio
async def test_the_git_path_reports_the_same_way(env, tmp_path, monkeypatch, capsys):
    import re
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fix: stale value"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    class GitArchivist(ScriptedArchivist):
        async def run_tooled(self, instruction, prompt, tools, budget, **kw):
            assert re.search(sha[:8], prompt)
            return await super().run_tooled(instruction, prompt, tools, budget, **kw)

    install(monkeypatch, GitArchivist([("write_incident", NEW_INCIDENT)], final_text="Wrote it."))
    await cli.cmd_learn(learn_args(postmortem_path=None, from_git="HEAD", repo=str(repo)))
    ok = capsys.readouterr().out
    assert "  -> OMEN-001" in ok and "model's note: Wrote it." in ok

    install(monkeypatch, GitArchivist(["deadline", ("write_incident", {**NEW_INCIDENT, "title": "second"})]))
    await cli.cmd_learn(learn_args(postmortem_path=None, from_git="HEAD", repo=str(repo)))
    refused = capsys.readouterr().out
    assert "-> no incident written (wall_clock_cap)" in refused and CLAIM not in refused


def test_seed_import_is_used():
    assert IncidentSeed  # keeps the shared test helpers' imports honest
