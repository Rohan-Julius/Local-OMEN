"""`omen doctor`: is the store healthy? Read-only unless `--fix`.

Checks (production plan Phase 4):
  (a) SQLite is the source of truth, Chroma a derived index: every variant
      SQLite implies must be in Chroma with the same text. Reported as
      `missing` (expected, not indexed), `stale` (indexed under the right
      id but with different text) and `extra` (indexed, but SQLite has no
      such variant, e.g. a forgotten incident or a form that was removed);
  (b) Ollama is reachable and both configured models are pulled;
  (c) the schema version;
  (d) the ledger size.

`diagnose` is pure: the Chroma contents and the Ollama answer are passed
in, so it is tested without Chroma, Ollama or a model.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

from omen import config, store, vectors
from omen.contracts import Incident


@dataclass
class Report:
    n_incidents: int
    n_variants: int  # what SQLite says should be indexed
    n_indexed: int
    schema_version: int
    latest_schema_version: int
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    ollama_error: str | None = None
    models_missing: list[str] = field(default_factory=list)

    @property
    def drift(self) -> bool:
        return bool(self.missing or self.stale or self.extra)

    @property
    def problems(self) -> list[str]:
        out = []
        if self.drift:
            out.append(f"index drift: {len(self.missing)} missing, {len(self.stale)} stale, {len(self.extra)} extra")
        if self.ollama_error:
            out.append(f"Ollama: {self.ollama_error}")
        out += [f"model not pulled: {m} (run `ollama pull {m}`)" for m in self.models_missing]
        if self.schema_version != self.latest_schema_version:
            out.append(f"schema version {self.schema_version}, expected {self.latest_schema_version}")
        return out

    @property
    def ok(self) -> bool:
        return not self.problems


def _expected_variants(incidents: list[Incident]) -> dict[str, str]:
    return {vid: text for incident in incidents for vid, _label, text in vectors._variants(incident)}


def _has_model(wanted: str, available: list[str]) -> bool:
    """`ollama list` shows `embeddinggemma:latest` for a model configured as
    `embeddinggemma`."""
    return wanted in available or (":" not in wanted and f"{wanted}:latest" in available)


def diagnose(
    conn,
    indexed: dict[str, str],
    ollama_models: list[str] | None,
    ollama_error: str | None = None,
) -> Report:
    """`ollama_models` is the list of pulled model names, or None if Ollama
    could not be asked (then `ollama_error` says why)."""
    incidents = store.list_incidents(conn)
    expected = _expected_variants(incidents)
    report = Report(
        n_incidents=len(incidents),
        n_variants=len(expected),
        n_indexed=len(indexed),
        schema_version=store.schema_version(conn),
        latest_schema_version=store.SCHEMA_VERSION,
        missing=sorted(v for v in expected if v not in indexed),
        stale=sorted(v for v in expected if v in indexed and indexed[v] != expected[v]),
        extra=sorted(v for v in indexed if v not in expected),
        ollama_error=ollama_error,
    )
    if ollama_models is not None:
        report.models_missing = [m for m in (config.LLM_MODEL, config.EMBED_MODEL) if not _has_model(m, ollama_models)]
    return report


def probe_ollama(host: str | None = None, timeout: float = 5.0) -> tuple[list[str] | None, str | None]:
    """(model names, None) if Ollama answers, else (None, reason)."""
    host = host or config.OLLAMA_HOST
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])], None
    except Exception as e:  # unreachable, refused, timeout, bad JSON: all "not usable"
        return None, f"not reachable at {host} ({type(e).__name__}: {e})"


def render(report: Report) -> list[str]:
    lines = [
        f"ledger:  {report.n_incidents} incident(s), {report.n_variants} variant(s) expected in the index",
        f"schema:  version {report.schema_version} (latest {report.latest_schema_version})",
        f"index:   {report.n_indexed} indexed; {len(report.missing)} missing, {len(report.stale)} stale, {len(report.extra)} extra",
    ]
    for label, ids in (("missing", report.missing), ("stale", report.stale), ("extra", report.extra)):
        lines += [f"           {label}: {vid}" for vid in ids]
    if report.ollama_error:
        lines.append(f"ollama:  {report.ollama_error}")
    elif report.models_missing:
        lines.append(f"ollama:  reachable; missing model(s): {', '.join(report.models_missing)}")
    else:
        lines.append(f"ollama:  reachable; {config.LLM_MODEL} and {config.EMBED_MODEL} present")
    lines.append("status:  OK" if report.ok else "status:  PROBLEMS - " + "; ".join(report.problems))
    return lines
