# Omen: production-readiness plan

Branch: `production-hardening` (from `main` at `8448aed`).
Owner: S. Mohamed Faizal. Source of every item: the audit in `docs/complete info about project.md`
(Appendix A = docs vs code, Appendix B = bugs B1–B28). Item IDs below (B3, A1, …) refer to those appendices.

## Goal

"Production-ready" here means: a team can install Omen with one command, run it on any machine
(GPU or CPU) from any directory, trust that it cannot be turned against the host, trust that what
it reports is backed by evidence, and see a green CI run on every push. It does **not** mean adding
a web UI, multi-language scanning or a cloud backend. Those stay out of scope (`PLAN.md` cut list).

## Ground rules (apply to every phase)

1. **Keep the architecture.** Deterministic pipeline, Gemma via Ollama, SQLite source of truth,
   Chroma derived index, both runners. Fix and harden; do not redesign.
2. **Every fix gets a test.** A bug from Appendix B is fixed only when a test reproduces it first
   and passes after.
3. **Do not silently change retrieval behaviour.** Any change to prompts, embeddings, prefixes,
   `incidents.yaml` or `fixtures/` must be followed by `omen calibrate` and the fixture/triage
   gates, with before/after numbers written in the phase notes.
4. **Low commit frequency.** One commit per phase (message `phase N: <summary>`). Push only at the
   three milestones marked **[PUSH]**. One PR to `main` at the end.
5. **Two test tiers.** `unit` tests (no model, run everywhere and in CI) and `live` tests (need
   Ollama). Final live verification is done on a GPU machine (RTX 5050), because CPU timings make
   some live tests fail by design of the old budget.
6. **No secrets, no network calls** beyond `localhost` Ollama. Hardening tests must stay green.

## Phase overview

| Phase | Theme | Fixes | Done when |
|---|---|---|---|
| 0 | Baseline and test tiers | B19, B20 | `pytest -m "not live"` runs everywhere; baseline numbers recorded |
| 1 | Security and correctness | B1, B2, B3, B4, B5, B9, B10, B11 | every listed bug has a failing-then-passing test |
| 2 | Packaging, config, CI **[PUSH]** | B16, B18, B24, B28, cwd-relative paths | `pip install -e .` gives an `omen` command that works from any directory; CI green |
| 3 | Trustworthy verdicts | B6, B7, B12, B13, B14, B15, B17 | evidence citations are validated; audit trail covers both missions |
| 4 | Memory integrity | B8, B26, schema migrations | dedup merges instead of overwriting; ledger has history; schema is versioned |
| 5 | Reliability and scale **[PUSH]** | B5 follow-up, B27, PLAN caps | warm-up + retry; per-call timeouts; `--max-gated`; CPU profile; full suite passes on CPU |
| 6 | Honest evaluation | B21, threshold tuned on test set | held-out fixture set with reported precision/recall; prompt contamination removed |
| 7 | Usable outputs | Markdown report, PLAN features | Markdown/JSON/SARIF reports, `--fail-on` exit codes, pre-commit hook |
| 8 | Docs and release **[PUSH]** | A1–A29, B25 | docs match code; reference doc updated; CHANGELOG; PR opened |

---

## Phase 0: Baseline and test tiers

**Why first:** you cannot prove an improvement without a baseline, and CI needs tests that run
without a model.

- Register markers in `pytest.ini` (or `pyproject.toml`): `live` (needs Ollama), `slow`.
- Replace module-level `pytestmark = skipif(...)` in the test files with a per-test `@pytest.mark.live`
  plus a `conftest.py` hook that skips `live` tests when Ollama is unreachable. Pure tests in
  `test_report.py`, `test_hardening.py`, `test_demo_repo.py` must then run without Ollama (B19).
- Make `test_fixtures.py` and `test_triage.py` build a temporary seeded store instead of reading
  the real `omen_store/` (B20).
- Record the baseline in `docs/PRODUCTION_PLAN.md` § "Progress log": unit test count and result,
  live result if run, `omen calibrate` table.

**Exit criteria:** `pytest -m "not live"` passes with Ollama stopped; same total test count as
before (92) or more.

## Phase 1: Security and correctness

| ID | Fix | Test |
|---|---|---|
| B3 | `read_commit`/`read_diff`: validate `sha` with `git rev-parse --verify --end-of-options <sha>^{commit}` (or a strict hex/ref regex), and put `--end-of-options` before every model-supplied argument | `read_commit("--output=x")` returns an error and creates no file |
| B4 | `read_file` for `learn <postmortem>`: confine to the postmortem's own directory (or an explicit `--allow-dir`), and deny-list `.env`, `*.db`, `omen_store/`, `.git/` | reading `.env` or `omen_store/omen.db` is refused |
| B1 | Scout diff/since: use `git diff --name-only --relative` run from the scanned path (or map root-relative paths correctly) | changed `sub/a.py` is found when scanning `sub/` |
| B2 | Default scope includes staged (`--cached`) and untracked (`git ls-files --others --exclude-standard`) `.py` files, de-duplicated | staged and new files are scanned |
| B5 | `ToolBudget`: start the wall clock at the first tool call (or measure tool time only) and make it configurable; generation time gets its own timeout (Phase 5) | budget test with a slow first turn still allows the first tool call |
| B9 | `write_incident`: `existing_ref` must match `^OMEN-\d{3,}$` **and** exist; otherwise error | unknown ref is rejected |
| B10 | `ToolBudget.invoke`: mark exceptions with a non-null `note="error"`; `written_ref` ignores error steps | failed write → "No incident was written" |
| B11 | `_next_ref`: never reuse a ref (keep a counter in a `meta` table, or use max over a tombstone table) | forget newest + learn → new ref, not the old one |

**Exit criteria:** all unit tests pass; each fix has its test; `docs/complete info about project.md`
Appendix B marked "fixed in phase 1" for these IDs (doc edit happens in Phase 8; note it in the log).

## Phase 2: Packaging, configuration, CI **[PUSH]**

- Add `pyproject.toml`: package metadata, Python `>=3.11,<3.13`, dependencies (pin `google-genai`
  explicitly, B24), console script `omen = omen.cli:entrypoint`, optional `[dev]` extras
  (pytest, pytest-asyncio, ruff, mypy, pytest-cov). Keep `requirements.txt` as a lock-style pin list.
- Ship prompts as package data (`omen/prompts/`), loaded with `importlib.resources`, so the CLI works
  from any directory. Keep a top-level `prompts/` symlink or remove it and update docs.
- Store location: `OMEN_HOME` env var / `--store` flag, default `./omen_store` (backwards compatible).
- One config source: `OLLAMA_HOST` read from the environment in `config.py` and passed explicitly to
  the embedding call, the `ollama` client (`ollama.Client(host=...)`) and LiteLLM (`api_base`) (B16).
  Optional `omen.toml` for model names, threshold, caps.
- Lazy-import ADK only in commands that need an LLM (B28).
- Exit codes: 0 success, 1 user error (bad args, unknown ref, empty ledger), 2 findings at or above
  `--fail-on` (Phase 7), 3 runtime/model error (B18).
- Replace scattered `print` diagnostics with `logging` (keep user-facing output on stdout);
  `--verbose` / `--quiet`.
- CI: `.github/workflows/ci.yml` on push/PR: Python 3.11 on ubuntu and windows, `pip install -e .[dev]`,
  `ruff check`, `pytest -m "not live" --cov=omen`. Add the badge to the README.
- Add `ruff` config (and `mypy` on `omen/contracts.py`, `omen/tools.py`, `omen/store.py` to start).

**Exit criteria:** fresh clone → `pip install -e .[dev]` → `omen --help` works from another
directory; CI green on both OSes.

## Phase 3: Trustworthy verdicts

- **Evidence validation (B6):** before accepting `confirmed`, every `evidence_lines` entry must parse
  as `path:line`, point to an existing line, and be inside the chunk **or** inside a range the
  Investigator actually read (from the transcript). Otherwise downgrade to `unverified` with a reason.
- **Adjudicator blindness (B7):** remove "Triage already ruled…" from `prompts/adjudicator.txt` and
  keep Triage's `code_mechanism` out of anything the Adjudicator sees; extend the blindness test to
  cover the system instruction. Re-run the triage/adjudicator live tests and log the numbers.
- **Structured incident choice (B17):** add `incident_ref: Literal[<candidate refs>]` to the
  Adjudicator output (schema built per call), store that instead of `candidates[0]`.
- **Audit trail (B12–B15):** add a `note` column to `tool_calls`; derive `stopped_reason` from actual
  cap events, not from whether text exists; add `ms_scout`, `ms_adjudicate`; persist `learn` runs and
  their tool calls (`mission='learn'`).

**Exit criteria:** the CPU scenario from the audit (zero successful tool calls, chunk-line citations)
can no longer produce `confirmed`; schema migration from Phase 4 or a fresh DB works.

## Phase 4: Memory integrity

- **Merge on dedup (B8):** updating an incident keeps existing surface forms and adds new ones
  (de-duplicated), keeps `occurred_on`, and appends provenance instead of overwriting it
  (`sources` list or an `incident_history` table with the previous row as JSON).
- **History and undo:** `omen memory show <ref>`, `omen memory history <ref>`, `omen memory revert <ref>`.
- **Schema versioning:** `PRAGMA user_version` + a small ordered migration list in `store.py`; tests
  that migrate an old-format DB.
- **Incremental reindex (B26):** `write_incident` re-embeds only the changed incident's variants
  (delete by `incident_ref`, upsert new ones); `omen reindex` stays the full rebuild.
- **Integrity check:** `omen doctor` compares SQLite variants with Chroma ids and reports drift;
  also checks Ollama reachability and both models.

**Exit criteria:** learning a duplicate twice keeps all surface forms from both runs; `omen doctor`
reports clean after seed/learn/forget sequences.

## Phase 5: Reliability and scale **[PUSH]**

- **Warm-up (B27):** one throwaway generation at the start of LLM commands, with one retry on the
  known CUDA first-call failure.
- **Timeouts:** a per-LLM-call timeout (configurable) separate from the tool budget; a clear
  `stopped_reason="llm_timeout"`.
- **Caps from PLAN.md:** `--max-gated N` (default 8) and `--max-positives N` (default 2) for
  investigations, with skipped chunks reported, never silently dropped.
- **Hardware profiles:** `--profile gpu|cpu` (or auto-detect via `ollama ps`), where `cpu` raises the
  budget, turns `think` off for the Investigator/Archivist, and lowers `num_ctx` if needed.
- **Batch retrieval:** query Chroma once for all chunks instead of once per chunk.
- **Graceful errors:** Ollama down, model missing, or a schema-invalid reply produce a one-line
  message and exit code 3, not a traceback. One retry for a schema-invalid structured reply.

**Exit criteria:** with `--profile cpu`, the full test suite (including live) passes on the i7 laptop;
on the RTX 5050 the demo scan finishes within the `PLAN.md` target (~120 s) and all tests pass.

## Phase 6: Honest evaluation

- Split fixtures: `fixtures/calibration.yaml` (used for tuning) and `fixtures/heldout.yaml`
  (never used for tuning), at least 8 per class in the held-out set, written in different domains.
- Remove contamination (B21): replace the two worked hard negatives in `prompts/triage.txt` with new
  examples that are in neither fixture set.
- `omen eval` command: runs retrieval + Triage (+ optional full pipeline) on the held-out set and
  prints precision, recall, and per-stage confusion counts; writes `eval/results-<date>.json`.
- Record results for both runners and both hardware profiles in `docs/EVALUATION.md`.

**Exit criteria:** a held-out table exists with real numbers and their source files; the threshold
choice is re-justified on calibration data and reported on held-out data.

## Phase 7: Usable outputs

- `--format text|markdown|json|sarif` and `--output <file>` for `scan` (SARIF lets GitHub code
  scanning show findings inline).
- `--fail-on confirmed|unverified` → exit code 2, for CI and pre-commit use.
- `.pre-commit-hooks.yaml` and a documented pre-commit example (`omen scan --since HEAD --fail-on confirmed`).
- Example GitHub Actions workflow for a self-hosted GPU runner (documented, not run in our CI).

**Exit criteria:** a scan of `demo_repo` produces valid JSON and SARIF (schema-checked in a unit test
with a fake runner).

## Phase 8: Docs and release **[PUSH]**

- Fix every Appendix A item (A1–A29); mark unbuilt `PLAN.md` features as "not built" or "built in
  phase N".
- `scripts/build_demo_repo.py` that recreates `demo_repo`'s nested history (fix commit + decoys) so
  `learn --from-git` demos work from a clone (B25); document it in the README.
- README rewrite: install, quick start, CPU vs GPU, commands, outputs, troubleshooting.
- Add `CHANGELOG.md`, `SECURITY.md` (threat model summary, how to report), `CONTRIBUTING.md`
  (test tiers, calibration rule, commit policy).
- Update `docs/complete info about project.md`: fresh test counts, Appendix B status column,
  new commands, new design decisions.
- Open one PR `production-hardening → main` (on the team repo) with the phase-by-phase summary.

**Exit criteria:** no open Appendix A item; reviewer can follow the README from a fresh clone.

---

## Out of scope (deliberately)

Web UI, multi-language parsing, cloud models, fine-tuning, LLM-based Sifter, Chronicler persona.
Revisit only after Phase 8.

## Progress log

| Date | Phase | Result | Notes |
|---|---|---|---|
| 2026-11-12 | 0 | `pytest -m "not live"`: **65 passed, 27 deselected** (92 total, unchanged). Full suite with Ollama port 11434 blocked: **65 passed, 27 skipped**. `tests/test_fixtures.py` (live, embed-only): 3 passed against the temp seeded store. `omen calibrate`: identical to the audit's sweep (table below). | Machine: CPU-only i7 laptop, Ollama reachable. B19: `live`/`slow` markers registered in `pytest.ini` (`--strict-markers`); per-test `@pytest.mark.live` + skip hook in `tests/conftest.py`; the 6 pure tests in `test_report.py` (4), `test_hardening.py` (1), `test_demo_repo.py` (1) now run without Ollama. B20: `test_fixtures.py`/`test_triage.py` use the `seeded_store` fixture (temp SQLite + Chroma seeded from `incidents.yaml`). Full live suite not run (CPU: ~1.5 h); `test_triage.py` not run. |
| 2026-11-12 | 1 | `pytest -m "not live"`: **86 passed, 27 deselected** (113 total; 21 new tests). Live: `tests/test_tools.py` **39 passed** (incl. the live `search_memory`/`get_incident` test); `tests/test_runners.py -k tooled` **4 passed** in 1243.97 s on CPU (adk transcript 499 s, direct transcript 350 s, adk call cap 211 s, direct call cap 174 s). | Each of B1 B2 B3 B4 B5 B9 B10 B11 has a test that failed on the unfixed code (20 failed + 1 guard test passing) and passes after the fix. B3 also confirmed by hand: old `read_commit("--output=<path>")` wrote the file. Prompts, embeddings, `incidents.yaml`, `fixtures/` unchanged, so calibrate was not re-run. Schema: new `meta` table (`CREATE TABLE IF NOT EXISTS`, holds the ref high-water mark for B11); the real `omen_store/omen.db` got the empty table on first connect, 6 incidents intact. Two existing tests updated because they asserted the old behaviour (`test_budget_trips_wall_clock_ceiling`, B5; `test_budget_catches_tool_exceptions_and_caches_the_error`, B10). Appendix B of `docs/complete info about project.md` still to be marked "fixed in phase 1" for these IDs (Phase 8). New finding, not fixed: Scout drops files with non-ASCII names (git quotes them, e.g. `"caf\303\251.py"`), in every scope. |

Phase 0 calibration sweep (`python -m omen.cli calibrate`, real `omen_store/`, 2026-11-12):

| id | label | target | top candidates |
|---|---|---|---|
| TM1 | true_match | OMEN-001 | OMEN-001=0.526, OMEN-004=0.236 |
| TM2 | true_match | OMEN-002 | OMEN-002=0.499, OMEN-005=0.225 |
| TM3 | true_match | OMEN-003 | OMEN-003=0.360, OMEN-001=0.232 |
| TM4 | true_match | OMEN-004 | OMEN-004=0.389, OMEN-001=0.172 |
| HN1 | hard_negative | - | OMEN-001=0.343, OMEN-002=0.236 |
| HN2 | hard_negative | - | OMEN-002=0.464, OMEN-005=0.252 |
| HN3 | hard_negative | - | OMEN-003=0.433, OMEN-001=0.256 |
| HN4 | hard_negative | - | OMEN-006=0.225, OMEN-002=0.213, OMEN-004=0.205 |
| U1 | unrelated | - | OMEN-005=0.163, OMEN-003=0.133, OMEN-004=0.119 |
| U2 | unrelated | - | OMEN-004=0.178, OMEN-005=0.163, OMEN-001=0.131 |
| U3 | unrelated | - | OMEN-004=0.172, OMEN-003=0.135, OMEN-001=0.116 |
| U4 | unrelated | - | OMEN-003=0.302, OMEN-005=0.261, OMEN-001=0.253 |

| threshold | TP | HN | unrelated |
|---|---|---|---|
| 0.00–0.15 | 4/4 | 4/4 | 4/4 |
| 0.20 | 4/4 | 4/4 | 1/4 |
| 0.25 | 4/4 | 3/4 | 1/4 |
| 0.30 | 4/4 | 3/4 | 1/4 |
| **0.35 (current)** | **4/4** | **2/4** | **0/4** |
| 0.40 | 2/4 | 2/4 | 0/4 |
| 0.45 | 2/4 | 1/4 | 0/4 |
| 0.50 | 1/4 | 0/4 | 0/4 |
| 0.55–1.00 | 0/4 | 0/4 | 0/4 |
