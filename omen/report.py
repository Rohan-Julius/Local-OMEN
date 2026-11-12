"""Phase 9 (the Scribe's reporting half): per-stage timing and the final
terminal summary for `omen scan`. Persistence itself
(`store.start_run`/`finish_run`/`record_finding`/`record_tool_call`) is
called directly from `cli.py`, which already owns per-role progress
printing — this module owns only the timing/counting used to build both
the terminal summary and the `RunRecord` passed to `store.finish_run`,
so, per PLAN.md, "screen and DB timings come from the same measurements"
rather than two stopwatches that could quietly disagree. Markdown
rendering is out of scope here — PLAN.md defers it past the schedule
("Markdown report — already deferred; terminal only").
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from omen.contracts import Finding, SkippedChunk


class Stopwatch:
    """Accumulates wall-clock ms across possibly-many `lap()` blocks — a
    stage like Triage runs once per gated chunk, and its total belongs in
    one `ms_triage` figure, not the last chunk's alone."""

    def __init__(self) -> None:
        self.total_ms = 0

    @contextmanager
    def lap(self) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.total_ms += round((time.perf_counter() - start) * 1000)


@dataclass
class ScanStats:
    """Accumulated over one `omen scan` run. `as_run_fields()` feeds
    `store.finish_run` directly, and `print_summary` prints the same
    numbers — one set of measurements, not a screen copy and a DB copy
    that can drift apart."""

    n_files: int = 0
    n_chunks: int = 0
    n_cache_hits: int = 0
    n_gated: int = 0
    n_triaged: int = 0
    n_investigated: int = 0
    n_tool_calls: int = 0
    n_confirmed: int = 0
    ms_scout: int = 0
    ms_embed: int = 0
    ms_triage: int = 0
    ms_investigate: int = 0
    ms_adjudicate: int = 0
    ms_total: int = 0  # wall clock for the whole command, Scout included (audit B14)
    skipped: list = field(default_factory=list)  # list[SkippedChunk]: cap-skipped chunks

    def as_run_fields(self) -> dict:
        return {
            "n_files": self.n_files,
            "n_chunks": self.n_chunks,
            "n_cache_hits": self.n_cache_hits,
            "n_gated": self.n_gated,
            "n_triaged": self.n_triaged,
            "n_investigated": self.n_investigated,
            "n_tool_calls": self.n_tool_calls,
            "n_confirmed": self.n_confirmed,
            "ms_scout": self.ms_scout,
            "ms_embed": self.ms_embed,
            "ms_triage": self.ms_triage,
            "ms_investigate": self.ms_investigate,
            "ms_adjudicate": self.ms_adjudicate,
            "ms_total": self.ms_total,
            "n_skipped": len(self.skipped),
        }


def print_skipped(skipped: list[SkippedChunk]) -> None:
    """Chunks a cap kept from being triaged or investigated. They are stored
    in `skipped_chunks` too: a cap must never hide a candidate."""
    triage = [s for s in skipped if s.stage == "triage"]
    investigation = [s for s in skipped if s.stage == "investigation"]
    print(f"skipped {len(skipped)} chunk(s) because of caps (stored in `skipped_chunks`, raise the cap to process them):")
    for label, group in (("not triaged", triage), ("Triage-positive, not investigated", investigation)):
        if group:
            print(f"  {label}: {len(group)}")
            for s in group:
                print(f"    {s.file_path}:{s.symbol or '?'}  similarity={s.similarity:.3f}  {s.reason}")


def print_summary(stats: ScanStats, findings: list[Finding]) -> None:
    print("\n" + "=" * 60)
    print(
        f"scan complete in {stats.ms_total / 1000:.1f}s — {stats.n_chunks} chunk(s), "
        f"{stats.n_gated} gated, {stats.n_triaged} triaged, {stats.n_investigated} investigated, "
        f"{stats.n_tool_calls} tool call(s)"
    )
    print(
        f"  scout {stats.ms_scout / 1000:.1f}s  embed {stats.ms_embed / 1000:.1f}s  "
        f"triage {stats.ms_triage / 1000:.1f}s  investigate {stats.ms_investigate / 1000:.1f}s  "
        f"adjudicate {stats.ms_adjudicate / 1000:.1f}s"
    )

    if stats.skipped:
        print_skipped(stats.skipped)

    if not findings:
        print("no findings.")
        return

    for f in findings:
        span = f"{f.start_line}-{f.end_line}" if f.start_line is not None and f.end_line is not None else "?"
        print(f"\n[{f.verdict.upper()}] {f.incident_ref}  {f.file_path}:{f.symbol or '?'} ({span})")
        print(f"  mechanism: {f.code_mechanism}")
        if f.evidence:
            print(f"  evidence: {f.evidence}")
        if f.downgrade_reason:
            print(f"  downgraded from confirmed: {f.downgrade_reason}")
