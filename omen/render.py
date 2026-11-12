"""Scan report formats (production plan Phase 7): `omen scan --format
text|markdown|json|sarif`. Pure functions from a `report.ScanReport` to a
string; no model, no I/O. `text` is `report.summary_lines`, the terminal
summary that existed before Phase 7.

- **json**: everything a script needs, stable keys, `schema_version` 1. Each
  entry of `findings` is exactly a `contracts.Finding` (`Finding(**entry)`
  rebuilds it), so the output round-trips.
- **markdown**: the same content for a person: a pull-request comment, a
  wiki page or an issue.
- **sarif**: SARIF 2.1.0 for GitHub code scanning and other SARIF viewers.
  One rule per incident in the ledger; one result per `confirmed` or
  `unverified` finding, located at the chunk's file and line range, with the
  evidence in the message. A `rejected` finding is the Adjudicator saying
  "not a match", so it is not a code-scanning alert; it stays in the json
  and markdown reports and in the `findings` table.
"""
from __future__ import annotations

import hashlib
import json
from importlib import metadata

from omen import report as report_mod
from omen.contracts import Finding, Incident

FORMATS = ("text", "markdown", "json", "sarif")
SCHEMA_VERSION = 1
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_LEVEL = {"confirmed": "error", "unverified": "warning"}  # rejected findings are not alerts


def tool_version() -> str:
    try:
        return metadata.version("omen")
    except metadata.PackageNotFoundError:  # running from a checkout that was never installed
        return "0.1.0"


def _counts(findings: list[Finding]) -> dict[str, int]:
    return {v: sum(1 for f in findings if f.verdict == v) for v in ("confirmed", "unverified", "rejected")}


def _incident_map(rep: report_mod.ScanReport) -> dict[str, Incident]:
    return {i.ref: i for i in rep.incidents}


# ---------------------------------------------------------------------------
# json
# ---------------------------------------------------------------------------


def render_json(rep: report_mod.ScanReport) -> str:
    incidents = _incident_map(rep)
    referenced = sorted({f.incident_ref for f in rep.findings})
    doc = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "omen", "version": tool_version()},
        "run_id": rep.run_id,
        "repo": str(rep.repo_path),
        "scope": rep.scope,
        "runner": rep.runner,
        "llm_model": rep.llm_model,
        "embed_model": rep.embed_model,
        "summary": {**rep.stats.as_run_fields(), "findings": _counts(rep.findings)},
        "incidents": {
            ref: {"title": incidents[ref].title, "failure_mechanism": incidents[ref].failure_mechanism, "the_rule": incidents[ref].the_rule}
            for ref in referenced
            if ref in incidents
        },
        "findings": [f.model_dump() for f in rep.findings],
        "skipped": [s.model_dump() for s in rep.stats.skipped],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def _span(f: Finding) -> str:
    return f"{f.start_line}-{f.end_line}" if f.start_line is not None and f.end_line is not None else "?"


def render_markdown(rep: report_mod.ScanReport) -> str:
    incidents = _incident_map(rep)
    s = rep.stats
    counts = _counts(rep.findings)
    lines = [
        "# Omen scan report",
        "",
        f"- **Scope:** {rep.scope}",
        f"- **Run:** {rep.run_id or '-'}  ·  runner {rep.runner}  ·  model {rep.llm_model}",
        f"- **Pipeline:** {s.n_files} file(s), {s.n_chunks} chunk(s), {s.n_gated} cleared retrieval, {s.n_triaged} triaged, "
        f"{s.n_investigated} investigated, {s.n_tool_calls} tool call(s), {s.ms_total / 1000:.1f}s",
        f"- **Findings:** {counts['confirmed']} confirmed, {counts['unverified']} unverified, {counts['rejected']} rejected",
        "",
    ]
    shown = [f for f in rep.findings if f.verdict != "rejected"]
    if not rep.findings:
        lines += ["No findings.", ""]
    elif not shown:
        lines += ["No confirmed or unverified findings.", ""]
    for f in shown:
        inc = incidents.get(f.incident_ref)
        title = f" — {inc.title}" if inc else ""
        lines += [
            f"## {f.verdict.upper()}: {f.incident_ref}{title}",
            "",
            f"**Location:** `{f.file_path}` · `{f.symbol or '?'}` · lines {_span(f)}",
            f"**Confidence:** {f.confidence or '-'} · **Similarity:** {f.similarity:.3f} · **Tool calls:** {f.tool_calls if f.tool_calls is not None else '-'}",
            "",
            f"**Mechanism:** {f.code_mechanism or '-'}",
            "",
            f"**Reasoning:** {f.reasoning or '-'}",
            "",
            f"**Evidence:** {f.evidence or 'none cited'}",
        ]
        if f.downgrade_reason:
            lines += ["", f"> Downgraded from confirmed: {f.downgrade_reason}"]
        if inc:
            lines += ["", f"**Rule:** {inc.the_rule}"]
        lines.append("")
    rejected = [f for f in rep.findings if f.verdict == "rejected"]
    if rejected:
        lines += ["## Rejected by the Adjudicator", ""]
        lines += [f"- `{f.file_path}:{f.symbol or '?'}` ({_span(f)}) against {f.incident_ref}: {f.reasoning or '-'}" for f in rejected]
        lines.append("")
    if s.skipped:
        lines += [f"## Skipped because of caps ({len(s.skipped)})", ""]
        for k in s.skipped:
            similarity = f"{k.similarity:.3f}" if k.similarity is not None else "?"
            lines.append(f"- `{k.file_path}:{k.symbol or '?'}` similarity {similarity}: {k.reason}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# sarif
# ---------------------------------------------------------------------------


def _sarif_rule(ref: str, incident: Incident | None) -> dict:
    if incident is None:  # a finding whose incident was forgotten after the scan
        return {"id": ref, "shortDescription": {"text": ref}, "help": {"text": ref}}
    rule = {
        "id": ref,
        "name": incident.title,
        "shortDescription": {"text": incident.title},
        "fullDescription": {"text": incident.failure_mechanism},
        "help": {"text": incident.the_rule},
        "defaultConfiguration": {"level": "warning"},
        "properties": {"learned_by": incident.learned_by},
    }
    if incident.severity:
        rule["properties"]["incident_severity"] = incident.severity
    return rule


def _sarif_message(f: Finding, incident: Incident | None) -> str:
    title = f" ({incident.title})" if incident else ""
    parts = [f"{f.verdict.capitalize()} repeat of {f.incident_ref}{title}: {f.code_mechanism or 'no mechanism recorded'}."]
    parts.append(f"Evidence: {f.evidence or 'none cited'}.")
    if f.downgrade_reason:
        parts.append(f"Downgraded from confirmed: {f.downgrade_reason}.")
    if incident:
        parts.append(f"Rule: {incident.the_rule}")
    return " ".join(parts)


def render_sarif(rep: report_mod.ScanReport) -> str:
    incidents = _incident_map(rep)
    reported = [f for f in rep.findings if f.verdict in SARIF_LEVEL]
    refs = [i.ref for i in rep.incidents]
    refs += sorted({f.incident_ref for f in reported} - set(refs))
    index = {ref: n for n, ref in enumerate(refs)}

    results = []
    for f in reported:
        physical: dict = {"artifactLocation": {"uri": f.file_path.replace("\\", "/"), "uriBaseId": "%SRCROOT%"}}
        if f.start_line is not None and f.start_line >= 1:
            region = {"startLine": f.start_line}
            if f.end_line is not None and f.end_line >= f.start_line:
                region["endLine"] = f.end_line
            physical["region"] = region
        location: dict = {"physicalLocation": physical}
        if f.symbol:
            location["logicalLocations"] = [{"name": f.symbol, "kind": "function"}]
        results.append(
            {
                "ruleId": f.incident_ref,
                "ruleIndex": index[f.incident_ref],
                "level": SARIF_LEVEL[f.verdict],
                "message": {"text": _sarif_message(f, incidents.get(f.incident_ref))},
                "locations": [location],
                "partialFingerprints": {
                    "omenFinding/v1": hashlib.sha256(f"{f.incident_ref}|{f.file_path}|{f.symbol}".encode("utf-8")).hexdigest()[:32]
                },
                "properties": {
                    "verdict": f.verdict,
                    "confidence": f.confidence,
                    "similarity": round(f.similarity, 4),
                    "evidence": f.evidence,
                    "downgrade_reason": f.downgrade_reason,
                },
            }
        )

    doc = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "omen",
                        "version": tool_version(),
                        "rules": [_sarif_rule(ref, incidents.get(ref)) for ref in refs],
                    }
                },
                "results": results,
                "invocations": [{"executionSuccessful": True}],
            }
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------


def render(fmt: str, rep: report_mod.ScanReport) -> str:
    if fmt == "json":
        return render_json(rep)
    if fmt == "markdown":
        return render_markdown(rep)
    if fmt == "sarif":
        return render_sarif(rep)
    if fmt == "text":
        return "\n".join(report_mod.summary_lines(rep.stats, rep.findings)).lstrip("\n") + "\n"
    raise ValueError(f"unknown format {fmt!r}")
