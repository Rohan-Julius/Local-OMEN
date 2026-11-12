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
   gates, with before/after numbers written in the phase notes. **Tool docstrings and generated
   prompt text count as prompts** (the model sees both: docstrings become tool descriptions, and
   the CLI builds the user messages). Change them only to correct a factual error, and list each
   change in the Progress log.
4. **Low commit frequency.** One commit per phase (message `phase N: <summary>`). Push only after
   Phases 2, 4, 5 and 8 (marked **[PUSH]**). One PR to `main` at the end.
5. **Two test tiers.** `unit` tests (no model, run everywhere and in CI) and `live` tests (need
   Ollama). Final live verification is done on a GPU machine (RTX 5050), because CPU timings make
   some live tests fail by design of the old budget.
6. **No secrets, no network calls** beyond `localhost` Ollama. Hardening tests must stay green.

## Phase overview

| Phase | Theme | Fixes | Done when |
|---|---|---|---|
| 0 | Baseline and test tiers | B19, B20 | `pytest -m "not live"` runs everywhere; baseline numbers recorded |
| 1 | Security and correctness | B1, B2, B3, B4, B5, B9, B10, B11 | every listed bug has a failing-then-passing test |
| 2 | Packaging, config, CI **[PUSH]** | B16, B18, B24, B28, B29, cwd-relative paths | `pip install -e .` gives an `omen` command that works from any directory; CI green |
| 3 | Trustworthy verdicts | B6, B7, B12, B13, B14, B15, B17 (+ carry-over B30) | evidence citations are validated; audit trail covers both missions |
| 4 | Memory integrity **[PUSH]** | B8, B26, history/undo, `doctor`, schema v3 | dedup merges instead of overwriting; ledger has history; schema is versioned |
| 5 | Reliability and scale **[PUSH]** | B5 part 2 (per-LLM-call timeout), B27, PLAN caps | warm-up + retry; per-call timeouts; `--max-gated`; CPU profile; full suite passes on CPU |
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

**B5 is partial after Phase 1.** The wall clock now starts at the first tool call, so a slow first
model turn no longer uses up the budget, but every model turn after the first tool call still
counts against it. On CPU a slow second turn can still get the next tool call refused. The second
half is in Phase 5 ("B5 part 2").

**Found during Phase 2 review (B30):** `sifter.py` read git output with the locale encoding and without
`-z`, like Scout before B29. On Windows a commit subject with certain UTF-8 bytes made decoding fail,
`stdout` came back `None` and `learn --from-git` crashed; non-ASCII changed paths came back in git's
quoted form. Fixed in Phase 3 (carry-over).

**Found during Phase 1 (B29):** Scout drops `.py` files with non-ASCII names in every scope, because
git quotes such paths (`"caf\303\251.py"`) and the quoted line no longer ends in `.py`. Fixed in
Phase 2.

## Phase 2: Packaging, configuration, CI **[PUSH]**

- Add `pyproject.toml`: package metadata, Python `>=3.11,<3.13`, dependencies (pin `google-genai`
  explicitly, B24), console script `omen = omen.cli:entrypoint`, optional `[dev]` extras
  (pytest, pytest-asyncio, ruff, pytest-cov). Keep `requirements.txt` as a lock-style pin list.
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
- Add `ruff` config. (mypy moved to Phase 8, optional.)
- **B29:** Scout reads git paths unquoted (`-z` output) in every scope; test with a non-ASCII
  filename.
- `--since` includes untracked files too (same de-duplicated union as the default scope).
- Fix the `read_file` tool docstring ("relative to the project root" is wrong since B4); log it as
  a prompt change (ground rule 3).
- Fake the embedding calls in the two `tests/test_report.py` persistence tests so they run in the
  unit tier and CI covers run/finding/tool_call persistence.

**Exit criteria:** fresh clone → `pip install -e .[dev]` → `omen --help` works from another
directory; CI green on both OSes.

## Phase 3: Trustworthy verdicts

- **Evidence validation (B6):** before accepting `confirmed`, every `evidence_lines` entry must parse
  as `path:line`, point to an existing line, and be inside the chunk **or** inside a range the
  Investigator actually read (from the transcript). Otherwise downgrade to `unverified` with a reason.
- **Adjudicator blindness (B7):** remove "Triage already ruled…" from `omen/prompts/adjudicator.txt` and
  keep Triage's `code_mechanism` out of anything the Adjudicator sees; extend the blindness test to
  cover the system instruction. Re-run the triage/adjudicator live tests and log the numbers.
- **Structured incident choice (B17):** add `incident_ref: Literal[<candidate refs>]` to the
  Adjudicator output (schema built per call), store that instead of `candidates[0]`.
- **Audit trail (B12–B15):** add a `note` column to `tool_calls`; derive `stopped_reason` from actual
  cap events, not from whether text exists; add `ms_scout`, `ms_adjudicate`; persist `learn` runs and
  their tool calls (`mission='learn'`).

**Exit criteria:** the CPU scenario from the audit (zero successful tool calls, chunk-line citations)
can no longer produce `confirmed`; schema migration from Phase 4 or a fresh DB works.

## Phase 4: Memory integrity **[PUSH]**

- **Merge on dedup (B8):** updating an incident keeps existing surface forms and adds new ones
  (de-duplicated), keeps `occurred_on`, and appends provenance instead of overwriting it
  (`sources` list or an `incident_history` table with the previous row as JSON).
- **History and undo:** `omen memory show <ref>`, `omen memory history <ref>`, `omen memory revert <ref>`.
- **Schema versioning:** extend the minimal ordered migration list that Phase 3 adds to `store.py`
  (driven by a `schema_version` key in the existing `meta` table, added in Phase 1 for the B11 ref
  high-water mark); tests that migrate an old-format DB (including one with no `meta` table).
- **Incremental reindex (B26):** `write_incident` re-embeds only the changed incident's variants
  (delete by `incident_ref`, upsert new ones); `omen reindex` stays the full rebuild.
- **Integrity check:** `omen doctor` compares SQLite variants with Chroma ids and reports drift;
  also checks Ollama reachability and both models.

**Phase 3 decisions carried into this phase:** `findings.stopped_reason` is stored (schema v3), and
`PYTHONUTF8=1` is removed from the Windows CI job (Linux keeps it), so Windows encoding bugs like
B29/B30 show up in CI instead of being masked.

**Exit criteria:** learning a duplicate twice keeps all surface forms from both runs; `omen doctor`
reports clean after seed/learn/forget sequences.

## Phase 5: Reliability and scale **[PUSH]**

- **Warm-up (B27):** one throwaway generation at the start of LLM commands, with one retry on the
  known CUDA first-call failure.
- **B5 part 2: separate per-LLM-call timeout.** A per-LLM-call timeout (configurable) separate from
  the tool budget, so model generation time no longer counts against the tool wall clock; a clear
  `stopped_reason="llm_timeout"`.
- **Caps from PLAN.md:** `--max-gated N` (default 8) and `--max-positives N` (default 2) for
  investigations, with skipped chunks reported, never silently dropped.
- **Hardware profiles:** `--profile gpu|cpu` (or auto-detect via `ollama ps`), where `cpu` raises the
  budget, turns `think` off for the Investigator/Archivist, and lowers `num_ctx` if needed.
- **CPU time budget and the `slow` marker (Phase 3 decision):** the live tests that Phase 3 ran
  (Adjudicator, structured runners, tools) took about 40 minutes on CPU, and the Phase 4 Archivist
  tests add more. State a wall-clock budget for a CPU live run, mark each live test that exceeds a
  few minutes `slow` (the marker exists since Phase 0 and is applied to the longest ones), and
  document `pytest -m "live and not slow"` as the quick live tier. `--profile cpu` is what makes the
  full suite fit the budget.
  **Stated CPU budget (Phase 5, i7-1355U, `--profile cpu`, measured):**

  | Tier | Command | Budget |
  |---|---|---|
  | unit | `pytest -m "not live"` | under 2 min (measured 74 s, 343 tests) |
  | quick live | `pytest -m "live and not slow"` | under 30 min (not separately timed) |
  | full live | `pytest -m live` | about 2.5 h; the run stopped at 2 h 10 min with 24 of 26 tests done |

  One structured call takes 110-170 s on CPU, one Investigator turn up to ~2 min, and a full
  demo-repo scan 1662 s (triage 802 s, investigate 463 s, adjudicate 373 s). `slow` is applied to the
  Adjudicator end-to-end, the four Archivist tests, the demo-repo scan, the runners' determinism test
  and both Triage tests. The CPU profile's limits (600 s per model call, 1800 s overall) are about
  3.5x the slowest measured call and 2x the slowest measured investigation.
- **Batch retrieval:** query Chroma once for all chunks instead of once per chunk.
- **Archivist summary (Phase 4 diagnostic):** after a refused or failed write the model claimed it had
  recorded an incident. Print `Learned/updated <ref>` only from the written ref, show the model's own
  text only when a ref was written (labelled as the model's note), otherwise print
  `No incident was written (<stopped_reason>)`.
- **Scripted-Archivist unit test:** a scripted fake runner driven through `learn_from_postmortem` and
  `cmd_learn` (write success, refused write, merge via `existing_ref`), so CI covers the learn
  mission's write path without Ollama.
- **Graceful errors:** the generic exit-3 one-liner for unexpected errors already exists (Phase 2).
  What is left: a specific, actionable message for **"Ollama down"** (connection refused at
  `OLLAMA_HOST`) and **"model missing"** (name the `ollama pull` to run), and **one retry for a
  schema-invalid structured reply** before giving up.

**Exit criteria:** with `--profile cpu`, the full test suite (including live) passes on the i7 laptop;
on the RTX 5050 the demo scan finishes within the `PLAN.md` target (~120 s) and all tests pass.

## Phase 6: Honest evaluation

- Split fixtures: `fixtures/calibration.yaml` (used for tuning) and `fixtures/heldout.yaml`
  (never used for tuning), at least 8 per class in the held-out set, written in different domains.
- Remove contamination (B21): replace the two worked hard negatives in `omen/prompts/triage.txt` with new
  examples that are in neither fixture set.
- `omen eval` command: runs retrieval + Triage (+ optional full pipeline) on the held-out set and
  prints precision, recall, and per-stage confusion counts; writes `eval/results-<date>.json`.
- Record results for both runners and both hardware profiles in `docs/EVALUATION.md`.
- **Evidence rules in the Adjudicator prompt (Phase 3 decision):** tell the Adjudicator that only
  `path:line` citations count, and only for the chunk or lines the Investigator actually read (in the
  live Phase 3 run it put `"grep_symbol({...}) -> []"` in `evidence_lines`, which validation would
  downgrade). This is a behaviour-changing prompt edit, so make it here, with before/after
  numbers on the held-out set, not earlier.
- **Adjudicator hard-negative live test (Phase 3 decision):** the end-to-end HN test returns early
  whenever Triage rejects, so the Adjudicator is never exercised on a hard negative. Add a live test
  that feeds a hard-negative chunk and its investigation transcript straight to the Adjudicator
  (skipping Triage) and asserts it does not confirm.

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
- README rewrite: install, quick start, CPU vs GPU, commands, outputs, troubleshooting. The
  troubleshooting section must cover the **Windows 260-character path limit** (installing into a
  deep directory fails on a litellm data file unless long-path support is on or the venv path is
  short), and the commands section must say that **`calibrate` and `seed` need explicit paths** when
  run outside the checkout (`--fixtures-path`, the YAML path).
- README troubleshooting must also show **`omen doctor`** (what it checks, `--fix`, exit codes) and
  the Phase 5 profile flags (`--profile`, `OMEN_PROFILE`) and exit codes (0 / 1 / 3).
- Point the README **CI badge at `main`** after the merge (it targets `production-hardening` now).
- Optional: `mypy` on `omen/contracts.py`, `omen/tools.py`, `omen/store.py` (moved here from Phase 2).
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
| 2026-11-12 | 0–1 | Summary of Phases 0 and 1 (commits `30b1345`, `791102e`): unit tier 65 → **86 passed**, 27 live deselected (92 → 113 tests). Live: `test_tools.py` **39 passed**; `test_runners.py -k tooled` **4 passed** (1243.97 s, CPU); `test_fixtures.py` 3 passed. `omen calibrate` unchanged (0.35: TP 4/4, HN 2/4, unrelated 0/4). | Fixed: B19 B20 (Phase 0); B1 B2 B3 B4 B5 (partial) B9 B10 B11 (Phase 1). Found: B29. Not run: full live suite, `test_triage.py`. |
| 2026-11-12 | 2 | `pytest -m "not live"`: **126 passed, 25 deselected** (151 total; 38 new, 2 moved live → unit); same run with global/system git config hidden (as on CI): 126 passed. Coverage (unit tier) **87%**. `ruff check .`: clean. Live: `tests/test_tools.py` **39 passed**; extra check `test_runners.py -k matches_true_positive` **2 passed** (adk 270 s, direct 165 s, CPU) through the new host plumbing. `omen memory list`: 3.46 s → **1.42 s** (median of 3; ADK no longer imported). Fresh venv `pip install -e .` + run from another directory: `omen --help` 0, `memory list --store` 1 on empty / 0 after `seed`; wheel contains the 5 prompts and the console script. | Fixed: B16, B18, B24, B28, B29; cwd-relative prompts and store. Prompts moved with `git mv` to `omen/prompts/` (git blob ids, file sha256 and loaded-text sha256 identical before/after for all 5); empty unused `prompts/chronicler.txt` removed. **Prompt change (rule 3):** `read_file` tool docstring, `file_path` arg: "relative to the project root" → "relative to the postmortem's own folder" (factual fix after B4). No other prompt, embedding, `incidents.yaml` or fixture change, so calibrate not re-run. `--since` now includes untracked files. `test_demo_repo` Redis check rewritten in Python (no `grep` on Windows CI). CI not yet observed green at time of commit (see push notes). Found: on Windows without long-path support, installing into a deep directory fails on a litellm data file (path > 260 chars); not an Omen bug, document in Phase 8. |
| 2026-11-12 | 2 (CI) | GitHub Actions run **43195240312** on `production-hardening` (commit `e56c8fd`): **green on both OSes**. ubuntu-latest and windows-latest, Python 3.11: `ruff check .` clean; **126 passed, 25 deselected**; coverage **87%** on both. | Recorded here because the Phase 2 row was written before CI had finished. |
| 2026-11-12 | 3 | `pytest -m "not live"`: **183 passed, 26 deselected** (209 tests); coverage **91%**; `ruff check .` clean. Run against the Phase 2 code (a temporary worktree of `e56c8fd`), all 25 new or changed tests in the files that can run there failed, the 2 guard tests passed, and `test_evidence.py` (27 tests) and `test_adjudicator.py` fail at import (`omen.evidence`, `adjudicator_schema` do not exist there). Live (CPU): `tests/test_tools.py -m live` **1 passed**; `tests/test_adjudicator.py -m live` **4 passed** in 1282 s: end-to-end TP **confirmed** as OMEN-001 and kept by evidence validation (Investigator stopped on `wall_clock_cap` after 4 successful calls; cited lines 1-3 lie inside its `read_code` 1-50), end-to-end HN passed because Triage ruled NO_MATCH (Investigator/Adjudicator not reached), live structured Adjudicator chose **OMEN-001 over the first-listed OMEN-004 on both runners** (adk 174 s, direct 143 s); `tests/test_runners.py -k structured` **6 passed** in 1136 s (TP, HN, determinism, both runners). `omen calibrate`: unchanged (0.35: TP 4/4, HN 2/4, unrelated 0/4). | Fixed: **B6, B7, B12, B13, B14, B15, B17**; carry-overs **B30** (Sifter UTF-8 and `-z`), empty-ledger exit codes, CI actions v4/v5 → v7, plan edits. Appendix B of the reference doc has no status column, so it is not edited; the fixed IDs are listed here. **B6 decision:** chunk-line citations alone never confirm when the Investigator made no successful tool call (`evidence.validate_confirmed(..., allow_chunk_only=False)`, nothing sets it to True). Schema: version 2 via `meta.schema_version` (`tool_calls.note`, `runs.ms_scout`, `runs.ms_adjudicate`, `runs.n_written`, `findings.downgrade_reason`). Behaviour changes: `scan` on an empty ledger now exits 1 before embedding and records no run (before Phase 3 it exited 0 and recorded an empty run; `test_hardening` updated and moved to the unit tier); `memory list` on an empty ledger exits 0. Test infra: the conftest Ollama probe (1 s, once) marked a busy CPU Ollama unreachable and silently skipped the 6 runner tests on the first attempt; it now tries 3 times with a 5 s timeout, and the rerun above is the reported result. Observed: the live structured Adjudicator put `"grep_symbol({'symbol': 'cache_clear'}) -> []"` in `evidence_lines`; evidence validation would downgrade such a verdict (not a `path:line`). Prompt changes: see below. |
| 2026-11-12 | 4 | `pytest -m "not live"`: **230 passed, 26 deselected** (256 tests; 47 new); coverage **92%**; `ruff check .` clean. `omen calibrate`: **unchanged** (0.35: TP 4/4, HN 2/4, unrelated 0/4; all 12 per-fixture scores identical). Live (CPU): `tests/test_tools.py -m live` **1 passed** (4.8 s). **The 4 Archivist tests FAILED** — `test_archivist.py::abstracts_and_spans_technologies` 454.5 s, `::dedups_on_second_run` 270.7 s, `test_archivist_git.py::records_pre_change_failure_not_the_fix` 502.2 s, `::git_dedups_on_second_run` 357.6 s — all with "Archivist never wrote an incident". **Cause: B5 part 2 (Phase 5), not a Phase 4 regression**: a diagnostic run of the postmortem scenario with steps printed showed `read_file` ok, `search_memory` ok (5.5 s), then `write_incident` **refused** (`wall-clock ceiling exceeded`) because the model's turn between the two calls took more than the 90 s tool wall clock on CPU; the model then claimed it had recorded the incident. `stopped_reason=wall_clock_cap` and `written_ref=None` ("No incident was written") were reported correctly. Tests and prompts not edited. | Fixed: **B8, B26**; history/undo, `omen doctor`, schema v3. **Merge semantics (B8):** `write_incident` with `existing_ref` calls `store.merge_incident`. Surface forms = union, existing first and never dropped, new ones added in order, de-duplicated on whitespace-collapsed casefolded text, total capped at 12 (a ledger already over 12 keeps all its originals). Kept: title, failure_mechanism, what_happened, the_rule (curated text), `learned_by`/`source` (original provenance). Kept, filled only if blank: severity, occurred_on. Appended: the new source to `incidents.sources` (JSON list; deduplicated on source+learned_by; old rows read as one source). The model's text that was NOT applied is stored in the history row's `proposal`. Why: the ledger is what every later scan is judged against, a model's rephrase of a curated mechanism can move the incident in embedding space and silently change recall, and nothing is lost because the proposal is kept. Defaults I chose beyond the brief: `what_happened` is kept too (it describes the first case, and the second case's text is in `proposal`); `omen seed` still REPLACES (the curated path), writing a history row only if something changed. **History:** table `incident_history(ref, version, ts, action, snapshot JSON, source, proposal)`; actions create/update/forget/**revert** (revert added so an undo is itself undoable); snapshot = full state after the action (for forget: state when forgotten). Incidents written before v3 get a baseline version 1 (stamped with `created_at`) before their first change. **`memory forget` keeps history** (so forgetting is undoable); nothing deletes history. `memory revert <ref>` restores the previous version, or a forgotten incident's state, under the same ref (refs are never reused); `--to N` picks a version; the incident is re-indexed; if Ollama is down the ledger is restored and the message points at `omen doctor --fix`. **Index (B26):** `vectors.reindex_incident` embeds first, then deletes by `incident_ref` and upserts; `omen reindex` stays the full rebuild. Strong test: after seed, write new, merge ×2, forget, revert forgotten, revert merge, merge, forget, the Chroma ids, documents, metadata and vectors equal a full reindex (checked after every step; a mutant that skips the delete is caught). **Doctor:** ledger vs index (missing/stale/extra by variant id and text), Ollama reachability and both models, schema version, ledger size; exit 0 clean / 1 problems; `--fix` = full reindex when drifted; read-only, creates nothing. **Schema v3:** `incident_history`, `incidents.sources`, `findings.stopped_reason` (NULL before). v2→v3 upgrade with data, and a failing v3 rolled back, both tested. **CI:** `PYTHONUTF8` removed from the Windows job (Linux keeps it). Real-embedder check outside pytest (no generation): seed 6 → merge into OMEN-001 → new incident OMEN-007: merge 2.6 s vs full reindex 4.5 s, doctor clean, same 34 ids, min cosine(incremental, full) 1.0, so the incremental path is verified against the real embedding model, though not yet driven by the real LLM (see the Archivist failures). **Incident:** my first doctor tests called `vectors.reindex` before `--store` was applied and emptied the REAL Chroma index in `./omen_store` (the SQLite ledger was untouched); repaired with `omen doctor --fix` (30/30), and `tests/conftest.py` now points every test's default store at a temp dir (autouse). Phase 3 row: already present from the Phase 3 commit, not duplicated. |
| 2026-11-12 | 4 (CI) | GitHub Actions run **43217245500** on `production-hardening` (commit `e7770b8`): **green on both OSes**. ubuntu-latest and windows-latest, Python 3.11: `ruff check .` clean; **230 passed, 26 deselected**; coverage **92%** on both. Windows ran without `PYTHONUTF8` and needed no code fix. | Recorded here because the Phase 4 row was written before CI had finished. The 4 Archivist live tests failed on CPU as B5 part 2 (see the Phase 4 row); fixed in Phase 5. |
| 2026-11-12 | 5 | `ruff check .` clean; `pytest -m "not live"`: **343 passed, 26 deselected, coverage 95%** (74 s). `omen calibrate` unchanged (4/4 TP, 2/4 HN, 0/4 unrelated at 0.35). `omen doctor` on the real store: OK (schema 4, 30 indexed). Live suite with `OMEN_PROFILE=cpu`, **stopped by the user after 2 h 10 min to save time**: 24 of 26 ran; **23 passed, 1 failed**. Passed: Adjudicator end-to-end (both) and structured (adk, direct), all 4 Archivist tests (the Phase 4 B5 failures), demo-repo scan (confirmed OMEN-001, 1662 s, 4 tool calls, `model_finished`), all 3 fixture tests, the librarian test, 9 of 10 runner tests, `test_search_memory_and_get_incident`. **Failed: `test_run_tooled_produces_a_sensible_transcript[direct]`** (see below). **Not rerun: stopped by the user to save time:** `test_triage.py::test_triage_precision_recall` (was running) and `test_triage_never_flags_unrelated`. Per-test times were not captured (the durations table prints only at the end of a run); the demo scan's 1662 s is from its own summary. | The failure's traceback was lost with the killed run, so its cause is **not confirmed**. Evidence: the same loop passed `test_run_tooled_respects_a_tight_call_cap[direct]` and the adk variant of this test passed, so a broken direct loop is unlikely. The likeliest cause is the `stopped_reason == model_finished` assertion: since B13 a run that hits the 6-call cap reports `call_cap` even when it wrote a summary, and a `think=False` model can over-call tools. A model-turn timeout (`llm_timeout`) is the other candidate. Next live run: the assertion now prints the transcript. Triage tests still need a live run. |
| 2026-11-12 | 6 | `ruff check .` clean; `pytest -m "not live"`: **373 passed, 30 deselected** (403 total; 30 new unit tests, 4 new live tests). **No live or model test was run** (user override: short on time), and `omen calibrate` was not re-run because the calibration fixtures' content is unchanged (a rename plus a comment; the test `test_calibration_set_is_the_original_twelve` pins the ids). | Built: `fixtures/calibration.yaml` (the 12, moved with `git mv`) and `fixtures/heldout.yaml` (24: 8 true_match, 8 hard_negative, 8 unrelated; different domains; no shared id, symbol, file path or code line with the calibration set; each true match targets a real incident and covers 5 of 6 incidents). **B21 fixed:** the two worked hard negatives in `omen/prompts/triage.txt` (calibration HN1 and HN2 verbatim) were replaced by two examples in neither file (see the Phase 6 prompt change below); `tests/test_evaluation.py` now fails if any fixture symbol or code line appears in any shipped prompt, and a check proves it flags the old prompt. `omen eval [--fixtures-path] [--full] [--retrieval-only] [--runner] [--results-dir]` (`omen/evaluation.py` plus `cli.cmd_eval`): cumulative per-stage confusion counts, precision, recall and per-class survivors; writes `eval/results-<date>.json` (never overwrites; records fixture SHA-256, threshold, runner, profile, models and prompt hashes); `--full` writes each case's code to a temporary repo for the Investigator's tools. Unit-tested end to end with a fake runner and a fake embedder (both runners, retrieval-only, no warm-up when nothing is gated, user errors, empty ledger). New live test `test_live_adjudicator_does_not_confirm_a_hard_negative` (Adjudicator called directly with a hard-negative chunk and a transcript, 2 chunks x 2 runners, marked `slow`): **not run**. `calibrate`, `test_fixtures`, `test_triage` point at `calibration.yaml`. `docs/EVALUATION.md` written with an empty results table. **Not done, on purpose:** the Phase 6 plan item "evidence rules in the Adjudicator prompt" (behaviour-changing; the brief said to leave the other prompts alone; needs before/after numbers on the held-out set), and results "for both runners and both profiles" (need the model). All five pending items are in the list below. |
| 2026-11-12 | 7 | `ruff check .` clean; `pytest -m "not live"`: **414 passed, 30 deselected** (444 total; 41 new unit tests in `tests/test_outputs.py`). **No live or model test was run** (user override). | Built: `omen scan --format text|markdown|json|sarif`, `--output FILE`, `--fail-on confirmed|unverified` (exit code 2, `EXIT_FINDINGS`). New `omen/render.py` (pure renderers) and `report.ScanReport` / `report.summary_lines`; `cli.cmd_scan` is now a thin wrapper over `_run_scan`. **Output rules:** `text` is unchanged (progress, then the summary, on stdout); the three other formats print ONLY the document on stdout and send progress and errors to stderr; `--output` writes the document to a file (creating folders) and leaves stdout empty; an empty scan still emits a valid empty document; `--format/--output/--fail-on` with `--dry-run` or `--retrieval-only` is a user error (exit 1). **SARIF 2.1.0:** a rule per ledger incident, a result per `confirmed` (error) or `unverified` (warning) finding with file, line range, function name and the evidence in the message; `rejected` findings are not alerts (they stay in json/markdown and the database). **JSON** entries are exactly `Finding` models (round-trip tested). **`--fail-on`:** `unverified` means confirmed-or-unverified; `rejected` never trips; the report is written before the exit code is decided; a Triage-positive chunk skipped by `--max-positives` cannot trip it, and a stderr warning says so. Also: `.pre-commit-hooks.yaml` (`omen-scan`: `omen scan --since HEAD --fail-on confirmed`, `pass_filenames: false`), `docs/OUTPUTS.md` (formats, exit codes, pre-commit example, self-hosted GPU runner notes) and `docs/examples/omen-scan.workflow.yml` (documented, **not run**, kept outside `.github/workflows/`). Tests: every format with a fake runner; a structural SARIF checker (required fields, rule/ruleIndex consistency, uri and region validity) that is itself tested against 6 broken documents; JSON round trip; `--fail-on` truth table; the hook and the example workflow parse as YAML and their scan commands parse with the real CLI. **Not verified:** a real SARIF upload to GitHub, a real pre-commit run, a live `omen scan --format ...` against the model (added to the pending list as item 6). |

### Phase 3 prompt changes (ground rule 3)

On purpose, for B7 (Adjudicator and Investigator blindness to Triage). SHA-256 of the text as loaded
(`omen.prompts.load_prompt`, LF line endings; equal to the git blob content):

| prompt | before | after |
|---|---|---|
| `adjudicator.txt` | `ff58f856e09a947e7f1aa8a5d3962572bd0e5b355d5c4f871a6a58c47239df59` | `d30652c3ffc6de8d7a0582a4b4add54bf088552a5f73aae3d78eaa731d7240de` |
| `investigator.txt` | `b6a6e220fd837f254d0820ed6f1044564014880be817aeef97e1bb3c92e0a982` | `9a55bc188a5d6c203e4310b5017b923514dc96fecda1df82be3b667b92a206ae` |
| `triage.txt` (unchanged) | `3ccfe136ea5138671752a82fa69b07b353a90fb226ba16a0a4b976c6467ae9a6` | `3ccfe136ea5138671752a82fa69b07b353a90fb226ba16a0a4b976c6467ae9a6` |

```diff
--- a/omen/prompts/adjudicator.txt
+++ b/omen/prompts/adjudicator.txt
@@ -1 +1 @@
-You are the Adjudicator, the final judge in an institutional-memory system that catches code repeating a team's own past failures. Triage already ruled this chunk a plausible match, and the Investigator has since gathered evidence from the surrounding codebase. You do not see Triage's reasoning or its verdict — your ruling must rest only on the chunk itself, the candidate incident(s), and the investigation transcript in front of you.
+You are the Adjudicator, the final judge in an institutional-memory system that catches code repeating a team's own past failures. The Investigator has gathered evidence about this chunk from the surrounding codebase. Your ruling must rest only on the chunk itself, the candidate incident(s), and the investigation transcript in front of you.
@@ -11 +11 @@ You are the Adjudicator, the final judge in an institutional-memory system that
-State what the code can concretely do wrong, independent of the incident, before ruling — the same discipline as Triage, but now with the investigation's evidence in hand rather than the chunk alone.
+State what the code can concretely do wrong, independent of the incident, before ruling — with the investigation's evidence in hand rather than the chunk alone.
--- a/omen/prompts/investigator.txt
+++ b/omen/prompts/investigator.txt
@@ -1 +1 @@
-You are the Investigator, the second stage in an institutional-memory system. Triage has already flagged this code chunk as a plausible match against one or more past incidents. Your job is not to re-decide MATCH or NO_MATCH — that belongs to the Adjudicator, who will read your transcript afterward without seeing Triage's reasoning. Your job is to gather the specific evidence that lets the Adjudicator decide correctly.
+You are the Investigator, the second stage in an institutional-memory system. This code chunk was retrieved as resembling one or more past incidents. Your job is not to decide MATCH or NO_MATCH — that belongs to the Adjudicator, who will read your transcript afterward. Your job is to gather the specific evidence that lets the Adjudicator decide correctly.
@@ -12 +12 @@ You have four tools:
-- `search_memory(query)` — semantic search over the incident ledger, in case the chunk resembles a different incident than the ones Triage surfaced.
+- `search_memory(query)` — semantic search over the incident ledger, in case the chunk resembles a different incident than the candidate incidents you were given.
```

Generated prompt text changed in the same phase:

- **Investigator user prompt** (`cli._format_investigator_prompt`). Before:
  `Triage ruled MATCH on this chunk against candidate incident(s) <refs>.` /
  `Triage's stated mechanism: <Triage code_mechanism>` / the code chunk / `Investigate whether this chunk
  actually shares the candidate incident's failure mechanism.` After: the code chunk, the
  `CANDIDATE INCIDENTS:` block (ref, title, failure_mechanism, the_rule: the same block the Adjudicator
  sees), then `Investigate whether this chunk actually shares a candidate incident's failure mechanism.`
- **Adjudicator output schema** (B17): new required field `incident_ref` (enum of the call's candidate
  refs) with the description "The ref of the candidate incident this verdict is about, exactly as listed
  under CANDIDATE INCIDENTS." It is the last property, so the order of the existing fields is unchanged.

Retrieval inputs (embeddings, prefixes, `incidents.yaml`, `fixtures/`, `triage.txt`) are unchanged;
`omen calibrate` was re-run anyway and is identical. The Adjudicator live tests were re-run (row above).

### Phase 6 prompt change (ground rule 3)

On purpose, for B21. SHA-256 of the text as loaded (`omen.prompts.sha256`, LF line endings):

| prompt | before | after |
|---|---|---|
| `triage.txt` | `3ccfe136ea5138671752a82fa69b07b353a90fb226ba16a0a4b976c6467ae9a6` | `12a45e149ff572de30813909da523be1dc5b78ee9433ec8fd7a11510c1930b88` |
| `investigator.txt`, `adjudicator.txt` | unchanged | unchanged |

Only the section "Two worked examples of hard negatives" changed. The old examples were calibration HN1 (a `cachetools.TTLCache` thumbnail cache against OMEN-001)
and HN2 (`connect_with_backoff` against OMEN-002). The new ones target OMEN-003 (a log line that names an API key but writes only an opaque id) and OMEN-004
(a check-then-act on a local `set`, with no shared state). Neither chunk is in `calibration.yaml` or `heldout.yaml`. The instruction text around the examples, the
required output, and the "default is NO_MATCH" rules are untouched.

```diff
@@ -12,26 +12,23 @@ Before you decide MATCH or NO_MATCH, state what this code chunk can concretely d
 
 ## Two worked examples of hard negatives — study these
 
-Example 1. Incident: "An authorization decision is cached with no invalidation path tied to the permission-changing event, so a revoked permission keeps succeeding." Code chunk:
-    _thumb_cache = cachetools.TTLCache(maxsize=500, ttl=3600)
-    def get_thumbnail(image_id):
-        if image_id in _thumb_cache:
-            return _thumb_cache[image_id]
-        thumb = render_thumbnail(image_id)
-        _thumb_cache[image_id] = thumb
-        return thumb
-This is a real cache with no invalidation — but it caches a rendered thumbnail, not an authorization decision. There is no permission check anywhere in it, so there is nothing for a permission change to invalidate. Verdict: NO_MATCH. The shared vocabulary ("cache", "no invalidation") is a coincidence of implementation, not a shared mechanism.
-
-Example 2. Incident: "Failed requests are retried on a fixed, unjittered interval, so every waiting client retries in the same instant and the resulting spike re-triggers the outage." Code chunk:
-    def connect_with_backoff(max_attempts=5):
-        for attempt in range(max_attempts):
-            try:
-                return psycopg2.connect(DSN)
-            except OperationalError:
-                delay = min(2 ** attempt, 30) + random.uniform(0, 1)
-                time.sleep(delay)
-        raise ConnectionError("exhausted retries")
-This is a retry loop against the same kind of dependency — but it grows the delay exponentially and adds random jitter, which is exactly the fix the incident's rule calls for. Verdict: NO_MATCH. Retrying is not the failure; retrying with no backoff and no jitter is.
+Example 1. Incident: "A sensitive value (password, API key, token, connection string) flows unredacted into a log line, error message, or crash report, where it persists in a system with broader read access than the value itself warrants." Code chunk:
+    def refresh_api_key(key):
+        new_key = issue_key(key.owner_id)
+        logger.info("refreshed api key id=%s owner=%s ttl=%ss", new_key.id, key.owner_id, new_key.ttl)
+        return new_key
+This logs about an API key, and the words "api key" sound like the incident — but it writes only an opaque identifier, an owner id and a lifetime. The secret value itself never reaches the log line, so nothing sensitive persists anywhere readable. Verdict: NO_MATCH. Mentioning a credential is not leaking one.
+
+Example 2. Incident: "Code reads some state, decides an action is safe based on that read, then performs the action in a separate step with no lock or transaction spanning both — so two concurrent callers can both pass the check and both perform the action." Code chunk:
+    def unique_in_order(items):
+        seen = set()
+        result = []
+        for item in items:
+            if item not in seen:
+                seen.add(item)
+                result.append(item)
+        return result
+This is a check followed by an action, but `seen` is a local variable created inside the call and used by one thread of execution. No second caller can ever act between the check and the add, so there is no race. Verdict: NO_MATCH. A check-then-act sequence is only the failure when the state being checked is shared.
 
 ## What you will be shown
 
```

**Effect on the gates is unknown.** Calibration HN1 and HN2 are now unseen by the prompt, so the Triage gate (`test_triage.py`) is a stricter test than before and may report
different numbers. Pending live verification (Triage gate, calibrate).

### Pending live verification (cumulative)

Nothing in this list has been run since the change that made it necessary. Each item must be run on the GPU machine (or at least a live Ollama) before the branch is merged.

1. `tests/test_runners.py::test_run_tooled_produces_a_sensible_transcript[direct]`: failed in the Phase 5 CPU run; cause unconfirmed, probably the B13 `stopped_reason == model_finished` assertion.
2. The two `tests/test_triage.py` tests (`test_triage_precision_recall`, `test_triage_never_flags_unrelated`): not run since Phase 0, and now run against the new prompt.
3. The new Triage prompt from Phase 6 (sha256 `12a45e14...`): Triage gate and `omen calibrate`.
4. The Adjudicator hard-negative live test, `tests/test_adjudicator.py::test_live_adjudicator_does_not_confirm_a_hard_negative` (4 parametrizations).
5. `omen eval` on the held-out set (`fixtures/heldout.yaml`), both runners; fills the table in `docs/EVALUATION.md`.
6. (Phase 7) A live `omen scan --format json|sarif|markdown --fail-on confirmed` on `demo_repo`, and the SARIF file in a real viewer or `upload-sarif`; the pre-commit hook in a real repository. Renderers and exit codes are unit-tested with a fake runner only.

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
