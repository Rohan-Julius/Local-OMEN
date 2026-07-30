"""Pydantic contracts shared across the pipeline.

No third-party imports beyond pydantic and the standard library — every
other module imports this one (PLAN.md: "contracts.py first — no
dependencies, everything imports it").
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Memory: incidents.yaml, SQLite `incidents` + `surface_forms`
# ---------------------------------------------------------------------------


class IncidentSeed(BaseModel):
    """One entry in incidents.yaml, and the payload behind `write_incident`."""

    ref: str
    kind: Literal["incident", "rule"] = "incident"
    title: str
    failure_mechanism: str
    what_happened: str
    the_rule: str
    severity: str | None = None
    occurred_on: str | None = None
    source: str | None = None
    learned_by: str = "seed"
    surface_forms: list[str] = Field(default_factory=list)


class Incident(IncidentSeed):
    """A stored incident, as read back from SQLite."""

    id: int
    created_at: str


# ---------------------------------------------------------------------------
# Scout / Librarian (scan mission, retrieval side)
# ---------------------------------------------------------------------------


class Chunk(BaseModel):
    file_path: str
    start_line: int
    end_line: int
    symbol: str
    content: str
    content_hash: str


class RetrievalCandidate(BaseModel):
    """One incident scored against a chunk, after collapsing variants to
    the best-scoring variant per incident."""

    incident_ref: str
    similarity: float
    matched_variant: str


class GatedChunk(BaseModel):
    """A chunk that cleared the similarity floor and is headed to Triage."""

    chunk: Chunk
    candidates: list[RetrievalCandidate]


class FixtureCase(BaseModel):
    """One labeled chunk in fixtures/fixtures.yaml (Phase 4 calibration and
    the ongoing regression gate). `target_ref` is set only for
    true_match — hard_negative and unrelated fixtures should not clear
    the gate for any incident."""

    id: str
    label: Literal["true_match", "hard_negative", "unrelated"]
    target_ref: str | None = None
    file_path: str
    symbol: str
    code: str


# ---------------------------------------------------------------------------
# Triage / Investigator / Adjudicator (scan mission, judgment side)
# ---------------------------------------------------------------------------


class TriageVerdict(BaseModel):
    code_mechanism: str = Field(
        description="What this code can mechanically do wrong, stated before judging the match."
    )
    verdict: Literal["MATCH", "NO_MATCH"]
    confidence: Literal["low", "medium", "high"]
    reasoning: str


class ToolCallStep(BaseModel):
    """One tool round-trip in an Investigator transcript — also the shape
    persisted to SQLite's `tool_calls` table, the audit trail PLAN.md
    treats as the honest answer to "did the agent actually use tools?"."""

    tool_name: str
    args: dict = Field(default_factory=dict)
    result: str
    note: str | None = None  # e.g. a duplicate-call or cap notice from ToolBudget


class Transcript(BaseModel):
    """The Investigator's full trace for one chunk: every tool call plus
    its final free-text summary. The Adjudicator (Phase 7) reads this —
    blind to Triage's own reasoning and verdict — to reach a verdict with
    the surrounding code in hand rather than re-guessing from one chunk."""

    steps: list[ToolCallStep] = Field(default_factory=list)
    final_text: str = ""
    stopped_reason: Literal["model_finished", "call_cap", "wall_clock_cap"] = "model_finished"


class AdjudicatorVerdict(BaseModel):
    verdict: Literal["confirmed", "unverified", "rejected"]
    code_mechanism: str
    reasoning: str
    evidence_lines: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]


# ---------------------------------------------------------------------------
# Persistence records: SQLite `findings`, `runs`, `tool_calls`
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    id: int | None = None
    run_id: str
    incident_ref: str
    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    symbol: str | None = None
    similarity: float
    verdict: Literal["confirmed", "unverified", "rejected"]
    code_mechanism: str | None = None
    reasoning: str | None = None
    evidence: str | None = None
    confidence: str | None = None
    tool_calls: int | None = None


class RunRecord(BaseModel):
    run_id: str
    mission: Literal["scan", "learn"]
    repo_path: str | None = None
    scope: str | None = None
    llm_model: str | None = None
    embed_model: str | None = None
    runner: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    n_files: int | None = None
    n_chunks: int | None = None
    n_cache_hits: int | None = None
    n_gated: int | None = None
    n_triaged: int | None = None
    n_investigated: int | None = None
    n_tool_calls: int | None = None
    n_confirmed: int | None = None
    ms_embed: int | None = None
    ms_triage: int | None = None
    ms_investigate: int | None = None
    ms_total: int | None = None


class ToolCallRecord(BaseModel):
    id: int | None = None
    run_id: str
    chunk_symbol: str | None = None
    seq: int | None = None
    tool_name: str
    args_json: str | None = None
    result_summary: str | None = None
    ms: int | None = None
