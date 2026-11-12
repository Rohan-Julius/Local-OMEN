# production-hardening: pull request description (draft)

**Not opened.** This is a draft for the team to discuss first. Branch `production-hardening` into `main`
(base `8448aed`). Evidence for every item is in the progress log of `docs/PRODUCTION_PLAN.md`.

## Summary

Omen already ran end to end. A code audit (2026-11-12) found 28 bugs and 29 documentation mismatches
(`docs/complete info about project.md`, Appendices A and B). This branch fixes them in nine commits, one
per phase, without changing the architecture: a deterministic pipeline, Gemma through Ollama, SQLite as the
source of truth with Chroma as a derived index, both runners. "Production-ready" here means: installable
with one command, runs on a GPU or a CPU from any directory, cannot be turned against the host, reports
only what is backed by evidence, and has green CI. It does not add a web UI, other languages, or a cloud
backend.

## Phase by phase

| Phase | What changed | Fixes |
|---|---|---|
| 0 | `live` / `slow` test markers; model-free tests run without Ollama; fixture tests use a temporary store | B19, B20 |
| 1 | Security and correctness: git argument injection closed, `read_file` confined, Scout scope (subdirectory, staged, untracked), ref validation and reuse, tool-budget clock | B1 to B5 (part 1), B9 to B11 |
| 2 | `pip install -e .` gives an `omen` command from any directory; prompts are package data; `--store` / `OMEN_HOME`; one `OLLAMA_HOST`; lazy ADK import; exit codes; `logging`; **CI** (Ubuntu and Windows) | B16, B18, B24, B28, B29 |
| 3 | Verdicts are checked: evidence validation, a blind Adjudicator, a structured incident choice, a complete audit trail (also for `learn`) | B6, B7, B12 to B15, B17, B30 |
| 4 | Memory integrity: merge instead of overwrite, history and `memory revert`, `omen doctor`, incremental re-embed, versioned schema | B8, B26 |
| 5 | Reliability: warm-up and retry, per-model-call timeout, `--max-gated` / `--max-positives`, hardware profiles, batched retrieval, specific error messages | B5 (part 2), B27 |
| 6 | Honest evaluation: calibration and held-out fixture sets, prompt contamination removed, `omen eval`, `docs/EVALUATION.md` | B21 |
| 7 | Outputs: `scan --format text\|markdown\|json\|sarif`, `--output`, `--fail-on` (exit 2), pre-commit hook, CI example | |
| 8 | Docs and release: all 29 doc mismatches fixed, new README, CHANGELOG, SECURITY, CONTRIBUTING, demo history script | A1 to A29, B25 |

Two bugs were found along the way and fixed: B29 (Scout dropped files with non-ASCII names) and B30 (the
Sifter crashed on some UTF-8 commit subjects). Still open: B22 (Sifter keyword and path matching) and B23
(class attributes belong to no chunk); neither was in the plan.

## Tests

| | Result |
|---|---|
| `ruff check .` | clean |
| `pytest -m "not live"` | **425 passed**, 30 deselected (455 total), coverage **96%** |
| Audit baseline | 92 tests, 86 passed, no CI |
| CI (GitHub Actions) | green on Ubuntu and Windows for the commits of phases 2, 4, 5, 6 and 7; phase 8's run is recorded in the progress log |
| Live (model) tier | **CPU run only, in phase 5**: 23 of 26 passed in the 2 h 10 min that ran (1 failed, 2 not run). Nothing from phases 6 to 8 has run against a model |

Every bug fix has a test that was run against the unfixed code first and failed there (recorded per phase
in the progress log).

## Decisions worth discussing

- **Merge semantics when `learn` meets a near-duplicate:** surface forms are unioned (cap 12), curated
  text and original provenance are kept, the model's proposed rewrite is stored in the history row, not
  applied. `omen seed` still replaces.
- **A `confirmed` verdict needs seen evidence.** Chunk-line citations alone never confirm when the
  Investigator made no successful tool call. Stricter than before: more `unverified`, fewer false confirms.
- **SARIF omits `rejected` findings** (the Adjudicator said "not a repeat"); `--fail-on unverified` means
  "confirmed or unverified".
- **Machine formats print only the document on stdout**; progress goes to stderr.
- **The held-out set is never used for tuning**, and no fixture may appear in a prompt (a test enforces it).
- **Dev and CPU limits:** `--profile cpu` uses 600 s per model call and a 1800 s investigation deadline.

## Not done, on purpose

- The Phase 6 item "evidence rules in the Adjudicator prompt" (tell the model that only `path:line`
  citations count). It changes behaviour, needs before and after numbers on the held-out set, and the
  brief said to leave the other prompts alone. In the Phase 3 live run the Adjudicator put a tool-call
  string in `evidence_lines`; validation downgrades such a verdict, so this is a quality issue, not a safety one.
- Results of `omen eval` for both runners and both profiles (need the model).
- `mypy` (optional; not added).
- The README's CI badge still points at `production-hardening`; point it at `main` after the merge.

## Pending live verification

None of the following has been run since the change that requires it. They must be run on the GPU machine
(or any live Ollama) before the merge.

1. `tests/test_runners.py::test_run_tooled_produces_a_sensible_transcript[direct]`: failed in the phase 5 CPU
   run; the cause is unconfirmed, probably the B13 `stopped_reason == model_finished` assertion (the
   assertion now prints the transcript).
2. The two `tests/test_triage.py` tests: not run since phase 0, and now run against the new prompt.
3. The new Triage prompt from phase 6 (sha256 `12a45e149ff572de30813909da523be1dc5b78ee9433ec8fd7a11510c1930b88`):
   the Triage gate and `omen calibrate`.
4. The Adjudicator hard-negative live test, `tests/test_adjudicator.py::test_live_adjudicator_does_not_confirm_a_hard_negative`
   (four parametrizations).
5. `omen eval` on the held-out set, both runners; fills the table in `docs/EVALUATION.md`.
6. Phase 7 against a model: `omen scan --format json|sarif|markdown --fail-on confirmed` on `demo_repo`, a real
   SARIF viewer or `upload-sarif`, and the pre-commit hook in a real repository.
7. Phase 8: `python scripts/build_demo_repo.py` then `omen learn --from-git HEAD --repo demo_repo` against the
   model (the script is unit-tested; the learn run on its history is not).
8. GPU timings: the demo scan against the `PLAN.md` target of about 120 s, and the full live tier on the RTX 5050.

### Commands for the GPU machine

```
git checkout production-hardening && git pull
pip install -e ".[dev]"
ollama pull gemma4:12b-it-q4_K_M && ollama pull embeddinggemma
omen doctor
omen seed incidents.yaml && omen reindex
omen calibrate                                   # unchanged expected: 0.35 -> TP 4/4, HN 2/4, unrelated 0/4

pytest -m "not live"                             # 425 passed
pytest tests/test_runners.py -k "tooled and direct"                          # item 1
pytest tests/test_triage.py                                                  # items 2 and 3
pytest tests/test_adjudicator.py -k hard_negative                            # item 4
omen eval --runner adk    --profile gpu --full                               # item 5
omen eval --runner direct --profile gpu --full
omen scan demo_repo --all --format sarif --output omen.sarif --fail-on confirmed   # items 6 and 8
python scripts/build_demo_repo.py && omen learn --from-git HEAD --repo demo_repo --dry-run   # item 7
pytest -m "live and not slow"; pytest -m live    # the full tier
```

Then fill the table in `docs/EVALUATION.md`, add the results to the progress log, and update the checklist above.
