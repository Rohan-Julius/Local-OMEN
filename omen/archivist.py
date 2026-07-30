"""Mission 1 (PLAN.md): `omen learn` — the Archivist forms memories rather
than merely retrieving them. This module is the postmortem input path
(Phase 8a); the git input path (Phase 8b) reuses the same Archivist role,
dedup discipline, and `write_incident` tool through a different prompt and
tool set (`build_learn_git_tools`), not a different agent.

No new google.adk touchpoint: the Archivist is just another tools-agent
driven through the same `RoleRunner.run_tooled` the Investigator uses
(agents.py / runners.py), with its own prompt and its own three-tool set.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from omen import agents, tools as tools_mod
from omen.config import ARCHIVIST_THINK_DEFAULT
from omen.contracts import Transcript
from omen.tools import ToolBudget


def _build_postmortem_prompt(postmortem_path: str) -> str:
    return (
        f"A new postmortem is available at path {postmortem_path!r}. Read it with read_file, "
        "then decide whether it describes a failure worth remembering, check memory for a "
        "near-duplicate, and write the incident."
    )


async def learn_from_postmortem(
    runner,
    conn: sqlite3.Connection,
    allowed_root: Path,
    postmortem_path: str,
    think: bool = ARCHIVIST_THINK_DEFAULT,
    on_step: Callable | None = None,
    on_text: Callable[[str], None] | None = None,
) -> Transcript:
    """Drive one Archivist invocation over one postmortem document. Returns
    the tool-call transcript; the caller inspects it for a `write_incident`
    step to learn the assigned ref (PLAN.md: `write_incident` "Returns the
    assigned ref.")."""
    instruction = agents.load_prompt("archivist_postmortem.txt")
    learn_tools = tools_mod.build_learn_postmortem_tools(
        conn, allowed_root, source=f"postmortem:{postmortem_path}"
    )
    budget = ToolBudget()
    prompt = _build_postmortem_prompt(postmortem_path)
    return await runner.run_tooled(instruction, prompt, learn_tools, budget, think=think, on_step=on_step, on_text=on_text)


def _build_git_prompt(sha: str) -> str:
    return (
        f"Investigate commit {sha!r} using read_commit and read_diff. Describe the failure that "
        "existed BEFORE this commit — not the fix it applied — abstracted off its specific "
        "technology, then check memory for a near-duplicate before writing the incident. If the "
        "commit's diff doesn't reveal a clear pre-change failure, say so and do not write anything."
    )


async def learn_from_commit(
    runner,
    conn: sqlite3.Connection,
    repo_root: Path,
    sha: str,
    think: bool = ARCHIVIST_THINK_DEFAULT,
    on_step: Callable | None = None,
    on_text: Callable[[str], None] | None = None,
) -> Transcript:
    """Drive one Archivist invocation over one commit (PLAN.md Phase 8b:
    the git input path). Same role, same dedup discipline, same
    `write_incident` as `learn_from_postmortem` — only the prompt
    (`archivist_git.txt`, centered on describing the pre-change failure
    rather than the fix) and the tool set (`build_learn_git_tools`)
    differ."""
    instruction = agents.load_prompt("archivist_git.txt")
    learn_tools = tools_mod.build_learn_git_tools(conn, repo_root, source=f"git:{sha}")
    budget = ToolBudget()
    prompt = _build_git_prompt(sha)
    return await runner.run_tooled(instruction, prompt, learn_tools, budget, think=think, on_step=on_step, on_text=on_text)


def written_ref(transcript: Transcript) -> str | None:
    """The ref assigned by the last successful (non-capped, non-duplicate)
    `write_incident` call in a transcript, or None if the Archivist never
    wrote one."""
    for step in reversed(transcript.steps):
        if step.tool_name == "write_incident" and step.note is None:
            return step.result
    return None
