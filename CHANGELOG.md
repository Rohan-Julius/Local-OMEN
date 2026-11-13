# Changelog

All notable changes. This project has not cut a release yet; everything below is on the
`production-hardening` branch, ahead of `main`. Item IDs (B3, A7, ...) refer to the audit in
`docs/complete info about project.md` (Appendix A = docs versus code, Appendix B = bugs). The plan and the
evidence behind every entry are in `docs/PRODUCTION_PLAN.md`.

Live (model) verification of the later phases is **pending a final GPU run**; see
"Pending live verification" in `docs/PRODUCTION_PLAN.md`.

## [Unreleased] - production-hardening

### Phase 8: documentation and release
- README rewritten: install, quick start, CPU and GPU profiles, every command, outputs and exit
  codes, troubleshooting (Windows 260-character path limit, explicit paths for `seed`/`calibrate`/`eval`,
  `doctor`, profiles).
- Docs match the code: every item of Appendix A (A1 to A29) fixed; `PLAN.md` has an "As-built status"
  table and inline tags ("not built" / "built in phase N"); `CLAUDE.md` rewritten; stale comments fixed.
- `scripts/build_demo_repo.py` recreates `demo_repo`'s nested git history (B25).
- Added `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, `docs/PR_DESCRIPTION.md`.

### Phase 7: usable outputs
- `omen scan --format text|markdown|json|sarif`, `--output FILE`.
- `omen scan --fail-on confirmed|unverified` exits with code 2.
- SARIF 2.1.0: a rule per incident, a result per confirmed or unverified finding, evidence in the message.
- `.pre-commit-hooks.yaml` (`omen-scan`), `docs/OUTPUTS.md`, and a documented, not-run example workflow for
  a self-hosted GPU runner.

### Phase 6: honest evaluation
- Fixtures split: `fixtures/calibration.yaml` (tuning) and `fixtures/heldout.yaml` (24 cases, never used
  for tuning). `omen eval` reports per-stage precision, recall and confusion counts and writes
  `eval/results-<date>.json`.
- **B21:** the Triage prompt's two worked examples were calibration fixtures HN1 and HN2; replaced with
  examples that are in no fixture file. A test now fails if any fixture appears in any prompt.
- Adjudicator hard-negative live test (written, not run). `docs/EVALUATION.md`.

### Phase 5: reliability and scale
- **B27:** a warm-up call with one retry before the first model call.
- **B5 part 2:** a separate per-model-call timeout (`stopped_reason="llm_timeout"`); `--profile gpu|cpu|auto`
  and `OMEN_PROFILE`.
- `--max-gated` (default 8) and `--max-positives` (default 2); skipped chunks are reported and stored.
- One batched vector query per scan. Specific messages for "Ollama down" and "model missing"; one retry for
  a schema-invalid reply. The Archivist summary no longer claims a write that did not happen.
- Schema v4 (`skipped_chunks`).

### Phase 4: memory integrity
- **B8:** learning a near-duplicate merges into the existing incident instead of overwriting it.
- `omen memory show|history|revert`; `forget` keeps history so it is undoable. `omen doctor [--fix]`.
- **B26:** a write re-embeds only the changed incident. Versioned schema with migrations (v3).

### Phase 3: trustworthy verdicts
- **B6:** a `confirmed` verdict must cite `path:line` evidence the Investigator saw, after at least one
  successful tool call, or it is downgraded to `unverified` with the reason.
- **B7:** the Adjudicator and Investigator prompts no longer mention or receive Triage's output.
- **B17:** findings are recorded against the incident the Adjudicator chose.
- **B12 to B15:** the audit trail has notes, true `stopped_reason`, full timings, and covers `learn`.
- **B30:** the Sifter reads git output as UTF-8 with `-z`.

### Phase 2: packaging, configuration, CI
- `pyproject.toml`; `pip install -e .` gives an `omen` command that works from any directory (prompts
  are package data). **B24:** `google-genai` pinned.
- `OMEN_HOME` / `--store`; one `OLLAMA_HOST` for every caller (**B16**). ADK imported only by commands that
  run a model (**B28**).
- Exit codes: 0, 1 user error, 3 runtime error (**B18**). `logging` with `--verbose` / `--quiet`.
- GitHub Actions: `ruff check` and the unit tier on Ubuntu and Windows. **B29:** Scout reads git paths
  unquoted (non-ASCII file names).

### Phase 1: security and correctness
- **B3:** a model-chosen commit id can no longer be used as a git option (`--output=<path>` wrote a file).
- **B4:** `learn <postmortem>` may only read the postmortem's own folder; secrets, databases, `omen_store/`
  and `.git/` are always refused.
- **B1, B2:** Scout finds changed files when scanning a subdirectory, and includes staged and untracked files.
- **B5 (part 1), B9, B10, B11:** the tool wall clock starts at the first call; `existing_ref` is validated;
  a failed write is not reported as a written ref; refs are never reused.

### Phase 0: baseline and test tiers
- `live` and `slow` markers; model-free tests run without Ollama (**B19**); fixture tests use a temporary
  seeded store (**B20**).
