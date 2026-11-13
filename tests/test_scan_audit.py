"""Phase 3 (production plan): the scan pipeline end to end with a scripted
fake runner and fake embeddings (no model, no Ollama). Each test pins one
audit finding at the level where it was observed: what gets recorded in
`findings`, `runs` and `tool_calls`, and what the Investigator and
Adjudicator are shown.

- B6:  the CPU case (every tool call refused, chunk-line citations) must not
       end `confirmed`.
- B7:  neither the Investigator nor the Adjudicator sees Triage's verdict,
       mechanism or reasoning; neither role prompt mentions Triage.
- B12: tool_calls keep the note (duplicate, error, cap).
- B13: stopped_reason reports the cap even when the model still answered.
- B14: runs has ms_scout and ms_adjudicate, and ms_total covers every stage.
- B17: the finding is recorded against the incident the Adjudicator chose.
"""
from __future__ import annotations

import argparse
import re
import time

import pytest

from omen import cli, prompts, runners, store, tools as tools_mod, vectors
from omen.contracts import AdjudicatorVerdict, IncidentSeed, Transcript, TriageVerdict

TRIAGE_REASONING_MARKER = "TRIAGE_REASONING_MARKER_7f3a"
TRIAGE_MECHANISM_MARKER = "TRIAGE_MECHANISM_MARKER_9c1d"

CODE = (
    "import functools\n\n"
    "@functools.lru_cache(maxsize=1024)\n"
    "def has_access(user_id, resource_id):\n"
    "    return _compute_permission(user_id, resource_id)\n"
)


class ScriptedRunner:
    """Triage always says MATCH (with marker strings); the Investigator runs
    `investigate(tools_by_name, budget)` through the real ToolBudget; the
    Adjudicator returns `adjudicate(prompt)` as a dict. Every prompt is
    recorded."""

    def __init__(self, investigate, adjudicate, adjudicate_delay: float = 0.0):
        self.investigate = investigate
        self.adjudicate = adjudicate
        self.adjudicate_delay = adjudicate_delay
        self.calls: list[tuple[str, str, str]] = []  # (role, instruction, prompt)

    async def run_structured(self, instruction, prompt, schema, think=False):
        if schema is TriageVerdict:
            self.calls.append(("triage", instruction, prompt))
            return TriageVerdict(
                code_mechanism=TRIAGE_MECHANISM_MARKER, verdict="MATCH", confidence="high", reasoning=TRIAGE_REASONING_MARKER
            )
        assert issubclass(schema, AdjudicatorVerdict), schema
        self.calls.append(("adjudicator", instruction, prompt))
        time.sleep(self.adjudicate_delay)
        return schema.model_validate(self.adjudicate(prompt))

    async def run_tooled(self, instruction, prompt, tools, budget, think=True, on_step=None, on_text=None):
        self.calls.append(("investigator", instruction, prompt))
        wrapped, steps = tools_mod.budgeted_tools(tools, budget, on_step=on_step)
        final_text = self.investigate({fn.__name__: fn for fn in wrapped}, budget)
        return Transcript(steps=steps, final_text=final_text, stopped_reason=runners._stopped_reason(budget, steps, final_text))

    def prompts_for(self, role: str) -> list[tuple[str, str]]:
        return [(i, p) for r, i, p in self.calls if r == role]


def candidate_refs(prompt: str) -> list[str]:
    block = prompt.split("CANDIDATE INCIDENTS:", 1)[1]
    return re.findall(r"^(OMEN-\d{3,}):", block, flags=re.MULTILINE)


def verdict(verdict="confirmed", evidence=("permissions.py:3",), ref=None) -> dict:
    data = {
        "verdict": verdict,
        "code_mechanism": "caches a permission decision with no invalidation",
        "reasoning": "ruling on the chosen incident",
        "evidence_lines": list(evidence),
        "confidence": "high",
    }
    if ref:
        data["incident_ref"] = ref
    return data


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    """A repo with one permissions.py chunk, two seeded incidents, fake
    embeddings (everything matches everything at similarity 1.0), and an
    isolated store. Returns (repo, conn, run) where run(runner) scans."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "permissions.py").write_text(CODE)

    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "_embed", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real_connect(tmp_path / "test.db"))
    conn = real_connect(tmp_path / "test.db")
    for ref, title in (("OMEN-001", "Stale permission cache"), ("OMEN-004", "Check-then-act race")):
        store.upsert_incident(
            conn,
            IncidentSeed(
                ref=ref, title=title, failure_mechanism=f"{title} mechanism", what_happened="x", the_rule="y",
                surface_forms=[f"{title} surface form"],
            ),
        )
    vectors.reindex(store.list_incidents(conn))

    async def run(runner, **ns) -> None:
        monkeypatch.setattr(runners, "ADKRoleRunner", lambda: runner)
        options = dict(
            path=str(repo), since=None, all=False, dry_run=False, retrieval_only=False, runner="adk",
            no_warmup=True, profile="gpu", max_gated=8, max_positives=2,
        )
        args = argparse.Namespace(**{**options, **ns})  # tests override e.g. the caps
        await cli.cmd_scan(args)

    return repo, conn, run


def only_finding(conn):
    rows = conn.execute("SELECT * FROM findings").fetchall()
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b6_cpu_case_all_tool_calls_refused_is_not_confirmed(scan_env):
    _, conn, run = scan_env

    def investigate(t, budget):
        budget.wall_clock_seconds = -1  # every call is refused, as on the CPU run in the audit
        t["read_code"](file_path="permissions.py", start_line=1, end_line=5)
        t["get_incident"](incident_ref="OMEN-001")
        return "The investigation was cut short by the wall-clock ceiling."

    runner = ScriptedRunner(investigate, lambda p: verdict(ref=candidate_refs(p)[0], evidence=["permissions.py:3", "permissions.py:4-5"]))
    await run(runner)

    f = only_finding(conn)
    assert f["verdict"] == "unverified"
    assert "no successful tool call" in f["downgrade_reason"]
    assert f["stopped_reason"] == "wall_clock_cap"
    assert conn.execute("SELECT n_confirmed FROM runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_b6_transcript_backed_confirmation_is_kept(scan_env):
    _, conn, run = scan_env

    def investigate(t, budget):
        t["read_code"](file_path="permissions.py", start_line=1, end_line=5)
        return "read the decorator; no invalidation anywhere"

    runner = ScriptedRunner(investigate, lambda p: verdict(ref=candidate_refs(p)[0], evidence=["permissions.py:3: lru_cache"]))
    await run(runner)

    f = only_finding(conn)
    assert (f["verdict"], f["downgrade_reason"]) == ("confirmed", None)


@pytest.mark.asyncio
async def test_b7_investigator_and_adjudicator_never_see_triage(scan_env):
    _, _, run = scan_env

    def investigate(t, budget):
        t["read_code"](file_path="permissions.py", start_line=1, end_line=5)
        return "summary"

    runner = ScriptedRunner(investigate, lambda p: verdict(verdict="rejected", evidence=[], ref=candidate_refs(p)[0]))
    await run(runner)

    for role in ("investigator", "adjudicator"):
        [(instruction, prompt)] = runner.prompts_for(role)
        assert "triage" not in instruction.lower(), f"{role} system instruction mentions Triage"
        for text in (prompt, instruction):
            assert TRIAGE_REASONING_MARKER not in text
            assert TRIAGE_MECHANISM_MARKER not in text
            assert "Triage" not in text
        # Triage's verdict literal must not reach the generated prompt. (The
        # Investigator's instruction legitimately says "do not state a
        # MATCH/NO_MATCH verdict", so this is checked on the prompt only.)
        assert "MATCH" not in prompt
        assert "has_access" in prompt and "OMEN-001" in prompt  # the chunk and candidates are still there


def test_b7_role_prompt_files_do_not_mention_triage():
    for name in ("adjudicator.txt", "investigator.txt"):
        assert "triage" not in prompts.load_prompt(name).lower(), name


@pytest.mark.asyncio
async def test_b12_b13_tool_call_notes_are_persisted_and_the_cap_is_reported(scan_env, capsys):
    _, conn, run = scan_env

    def investigate(t, budget):
        t["read_code"](file_path="permissions.py", start_line=1, end_line=5)
        t["read_code"](file_path="permissions.py", start_line=1, end_line=5)  # duplicate
        t["read_code"](file_path="../../outside.py", start_line=1, end_line=5)  # escapes the repo: error
        budget.max_calls = 3
        t["grep_symbol"](symbol="cache_clear")  # refused: call cap
        return "I still wrote a summary after the cap."

    runner = ScriptedRunner(investigate, lambda p: verdict(verdict="rejected", evidence=[], ref=candidate_refs(p)[0]))
    await run(runner)

    rows = conn.execute("SELECT tool_name, note FROM tool_calls ORDER BY seq").fetchall()
    notes = [r["note"] for r in rows]
    assert notes[0] is None
    assert notes[1].startswith("repeat call")
    assert notes[2] == "error"
    assert "tool calls exceeded" in notes[3]
    assert "stopped_reason=call_cap" in capsys.readouterr().out  # was "model_finished" because text existed
    assert only_finding(conn)["stopped_reason"] == "call_cap"  # and it is stored with the finding (schema v3)


@pytest.mark.asyncio
async def test_b14_runs_time_every_stage_and_total_covers_them(scan_env):
    _, conn, run = scan_env

    def investigate(t, budget):
        t["read_code"](file_path="permissions.py", start_line=1, end_line=5)
        return "summary"

    runner = ScriptedRunner(
        investigate, lambda p: verdict(verdict="rejected", evidence=[], ref=candidate_refs(p)[0]), adjudicate_delay=0.05
    )
    await run(runner)

    r = conn.execute("SELECT * FROM runs").fetchone()
    stages = ["ms_scout", "ms_embed", "ms_triage", "ms_investigate", "ms_adjudicate"]
    assert all(r[s] is not None for s in stages)
    assert r["ms_adjudicate"] >= 40
    assert r["ms_total"] >= sum(r[s] for s in stages)


@pytest.mark.asyncio
async def test_b17_finding_is_recorded_against_the_incident_the_adjudicator_chose(scan_env):
    _, conn, run = scan_env
    chosen: list[str] = []

    def investigate(t, budget):
        t["read_code"](file_path="permissions.py", start_line=1, end_line=5)
        return "summary"

    def adjudicate(prompt):
        refs = candidate_refs(prompt)
        assert len(refs) == 2
        chosen.append(refs[-1])  # deliberately not the top retrieval candidate
        return verdict(evidence=["permissions.py:3"], ref=refs[-1])

    await run(ScriptedRunner(investigate, adjudicate))

    f = only_finding(conn)
    assert f["incident_ref"] == chosen[0]
