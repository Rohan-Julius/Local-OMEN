"""Scoring and reporting for `omen eval` (production plan Phase 6): pure
functions, no model, no I/O except `write_results`. The stages are run by
`cli.cmd_eval`; this module only turns per-case outcomes into confusion
counts, precision and recall, a printable table and a JSON file.

Metrics are CUMULATIVE, stage by stage: a chunk counts as "positive at stage
S" only if it survived every stage up to and including S, so each stage's row
answers "if the pipeline stopped here, what would it report?".

- positives are the `true_match` cases; `hard_negative` and `unrelated`
  cases are negatives (reported separately as survivors, never merged away);
- retrieval: a true match is positive if its TARGET incident clears the
  similarity floor; a negative is positive (a false positive) if ANY
  incident does;
- triage: positive at retrieval AND Triage ruled MATCH;
- full (`--full`): positive at triage AND the Adjudicator's final verdict,
  after evidence validation, is `confirmed` (for a true match: confirmed
  against the target incident).

Known limit: Triage's verdict names no incident (its reasoning does, in free
text), so a Triage MATCH on a true match is not checked against the target.
Only the full stage, whose schema carries `incident_ref`, checks that.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from omen.contracts import FixtureCase, RetrievalCandidate

CLASSES = ("true_match", "hard_negative", "unrelated")
STAGES = ("retrieval", "triage", "full")


@dataclass
class CaseResult:
    case: FixtureCase
    candidates: list[RetrievalCandidate]  # every collapsed candidate, before the floor
    threshold: float
    triage: dict | None = None  # {"verdict", "confidence", "code_mechanism", "reasoning"} once Triage ran
    full: dict | None = None  # {"raw_verdict", "verdict", "incident_ref", "downgrade_reason", ...} once the Adjudicator ran
    notes: list[str] = field(default_factory=list)

    @property
    def gated(self) -> list[RetrievalCandidate]:
        return [c for c in self.candidates if c.similarity >= self.threshold]

    def positive_at(self, stage: str) -> bool:
        if stage == "retrieval":
            return self._retrieval_positive()
        if stage == "triage":
            return self._retrieval_positive() and self.triage is not None and self.triage["verdict"] == "MATCH"
        if stage == "full":
            return (
                self.positive_at("triage")
                and self.full is not None
                and self.full["verdict"] == "confirmed"
                and (self.case.label != "true_match" or self.full["incident_ref"] == self.case.target_ref)
            )
        raise ValueError(f"unknown stage {stage!r}")

    def _retrieval_positive(self) -> bool:
        if self.case.label == "true_match":
            return any(c.incident_ref == self.case.target_ref for c in self.gated)
        return bool(self.gated)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def score_stage(results: list[CaseResult], stage: str) -> dict:
    """Confusion counts, precision, recall and per-class survivors for one stage."""
    survivors = {label: [r.case.id for r in results if r.case.label == label and r.positive_at(stage)] for label in CLASSES}
    totals = {label: sum(1 for r in results if r.case.label == label) for label in CLASSES}
    tp = len(survivors["true_match"])
    fn = totals["true_match"] - tp
    fp = len(survivors["hard_negative"]) + len(survivors["unrelated"])
    tn = totals["hard_negative"] + totals["unrelated"] - fp
    return {
        "survivors": survivors,
        "survivor_counts": {label: len(ids) for label, ids in survivors.items()},
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
    }


def build_report(
    results: list[CaseResult],
    stages: tuple[str, ...],
    *,
    fixtures_path: Path,
    meta: dict,
) -> dict:
    """The JSON-serialisable record written to eval/results-<date>.json."""
    fixtures_bytes = Path(fixtures_path).read_bytes()
    return {
        "date": date.today().isoformat(),
        "fixtures_path": str(fixtures_path),
        "fixtures_sha256": hashlib.sha256(fixtures_bytes).hexdigest(),
        "n_cases": {label: sum(1 for r in results if r.case.label == label) for label in CLASSES},
        **meta,
        "stages": {stage: score_stage(results, stage) for stage in stages},
        "cases": [
            {
                "id": r.case.id,
                "label": r.case.label,
                "target_ref": r.case.target_ref,
                "file_path": r.case.file_path,
                "symbol": r.case.symbol,
                "candidates": [{"incident_ref": c.incident_ref, "similarity": round(c.similarity, 4)} for c in r.candidates],
                "positive_at": {stage: r.positive_at(stage) for stage in stages},
                "triage": r.triage,
                "full": r.full,
                "notes": r.notes,
            }
            for r in results
        ],
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render(report: dict) -> list[str]:
    """The printable summary: one line per case, then one row per stage."""
    n = report["n_cases"]
    lines = [
        f"evaluation of {report['fixtures_path']}: {sum(n.values())} case(s) "
        f"({n['true_match']} true_match, {n['hard_negative']} hard_negative, {n['unrelated']} unrelated)",
        f"similarity floor {report['threshold']}  runner {report['runner']}  profile {report['profile'] or '-'}  "
        f"model {report['llm_model']}  embed {report['embed_model']}",
        "",
        f"{'id':5} {'label':13} {'target':9} {'top candidates':34} " + "  ".join(f"{s:9}" for s in report["stages"]),
    ]
    for c in report["cases"]:
        top = ", ".join(f"{x['incident_ref']}={x['similarity']:.3f}" for x in c["candidates"][:2]) or "(none)"
        marks = "  ".join(f"{'positive' if c['positive_at'][s] else '-':9}" for s in report["stages"])
        lines.append(f"{c['id']:5} {c['label']:13} {c['target_ref'] or '-':9} {top:34} {marks}")
    lines += [
        "",
        "Cumulative per stage; positive = reported as a match. Survivors are shown as hits/cases per class.",
        f"{'stage':10} {'true_match':>10} {'hard_neg':>9} {'unrelated':>10}   {'TP':>3} {'FP':>3} {'FN':>3} {'TN':>3}   {'precision':>9} {'recall':>7}",
    ]
    for stage, s in report["stages"].items():
        k, cm = s["survivor_counts"], s["confusion"]
        lines.append(
            f"{stage:10} {str(k['true_match']) + '/' + str(n['true_match']):>10} "
            f"{str(k['hard_negative']) + '/' + str(n['hard_negative']):>9} {str(k['unrelated']) + '/' + str(n['unrelated']):>10}   "
            f"{cm['tp']:>3} {cm['fp']:>3} {cm['fn']:>3} {cm['tn']:>3}   {_fmt(s['precision']):>9} {_fmt(s['recall']):>7}"
        )
    return lines


def write_results(report: dict, directory: Path) -> Path:
    """eval/results-<date>.json; a second run on the same day gets -2, -3, ...
    so no earlier result is overwritten."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"results-{report['date']}.json"
    n = 2
    while path.exists():
        path = directory / f"results-{report['date']}-{n}.json"
        n += 1
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
