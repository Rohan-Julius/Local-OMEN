# Evaluation

How Omen's detection quality is measured, on which data, and what the numbers do and do not
mean. Built in production-plan Phase 6. **The results table at the end is empty: it is to be
filled by the final live run on the GPU machine.** Nothing below reports a measured held-out number.

## Two fixture sets, two jobs

| File | Cases | Job | May it influence tuning? |
|---|---|---|---|
| `fixtures/calibration.yaml` | 12: 4 `true_match`, 4 `hard_negative`, 4 `unrelated` | Choose the similarity floor (`omen calibrate`); regression gate for retrieval (`tests/test_fixtures.py`) and Triage (`tests/test_triage.py`) | Yes. This is the tuning set |
| `fixtures/heldout.yaml` | 24: 8 `true_match`, 8 `hard_negative`, 8 `unrelated` | Report precision and recall (`omen eval`) | **No.** Never used to pick a threshold, edit a prompt or change `incidents.yaml` |

A number measured on the calibration set is a tuning result, not an evaluation: the threshold
(0.35) was chosen by looking at exactly those 12 chunks. Only held-out numbers may be quoted as
evaluation results.

### What each class means

- **true_match**: the code really shares the failure mechanism of its `target_ref` incident (see
  `incidents.yaml`). A reviewer who knew that incident would flag it.
- **hard_negative**: shares vocabulary or shape with an incident, but the mechanism is absent: the
  code already prevents it (a cache that is invalidated on the permission change, a retry with
  jittered backoff, an `UPDATE ... WHERE` guard) or it cannot occur (a "check then act" on a local
  variable). These are the cases that decide whether the tool is trusted.
- **unrelated**: nothing to do with any incident mechanism.

### How the held-out set was built

- Different domains from the calibration set. Calibration covers a permission lookup, a database
  reconnect loop, a login log line, seat booking, a thumbnail cache, stock counts and small
  utilities. The held-out set uses API tokens, webhooks, archive storage, coupons, chat rooms,
  reports, sensor data, billing clocks, session roles, routing, pid files and similar.
- The true matches cover five of the six incidents (OMEN-001 to OMEN-006), with two each for
  OMEN-004 and OMEN-005. Hard negatives cover every incident for which a safe variant exists.
- A unit test (`tests/test_evaluation.py`) checks, with no model: at least 8 cases per class; every
  `true_match` target exists in `incidents.yaml`; negatives have no target; no id, symbol, file path
  or substantial line of code is shared with the calibration set; each held-out case has its own
  file path (the full pipeline writes each case to its own file).
- **No fixture appears in any prompt** (audit B21). Until Phase 6 the Triage prompt's two worked
  examples were calibration cases HN1 and HN2 verbatim, so those two "hard negatives" were partly a
  test of memory. The examples were replaced by two that are in neither fixture file, and a test
  now fails if any fixture's symbol or code line appears in any shipped prompt.
- The set was written by the same agent that wrote the evaluation tooling, working from
  `incidents.yaml`. That is a real limit: the cases are plausible, not sampled from real repositories.

## Method

`omen eval [--fixtures-path FILE] [--full] [--retrieval-only] [--runner adk|direct] [--results-dir DIR]`
runs, for each case:

1. **Retrieval.** Embed the chunk, query the incident index, collapse to one score per incident,
   keep candidates at or above the floor (`config.SIMILARITY_THRESHOLD`, 0.35).
2. **Triage** (unless `--retrieval-only`): the real Triage prompt on every chunk that cleared the
   floor, exactly as `omen scan` builds it.
3. **Full pipeline** (`--full`): for Triage-positive cases, the Investigator (read tools over a
   temporary repo holding the case's code) and the Adjudicator, followed by evidence validation.

No caps apply (`--max-gated` and `--max-positives` are scan options, not eval options).

### Definitions

Positives are the `true_match` cases; `hard_negative` and `unrelated` are negatives, reported as
separate survivor counts as well as in the confusion table. Every stage is cumulative: a case is
**positive at a stage** only if it survived all earlier stages.

| Stage | A case is positive when |
|---|---|
| retrieval | a `true_match`: its **target** incident clears the floor; a negative: **any** incident clears it |
| triage | positive at retrieval **and** Triage's verdict is `MATCH` |
| full | positive at triage **and** the Adjudicator's final verdict, after evidence validation, is `confirmed` (for a `true_match`, confirmed against the target incident) |

`TP` = true matches that are positive, `FN` = true matches that are not, `FP` = negatives that are
positive, `TN` = negatives that are not. `precision = TP / (TP + FP)`, `recall = TP / (TP + FN)`;
either is reported as `n/a` when its denominator is zero.

### Output

`omen eval` prints one line per case and one row per stage, and writes
`eval/results-<date>.json` (a second run on the same day gets `-2`, `-3`; nothing is overwritten).
The file records the fixture file and its SHA-256, the threshold, runner, hardware profile, model
names, the SHA-256 of the Triage, Investigator and Adjudicator prompts, the per-class survivors and
confusion counts per stage, and, per case, the candidates with scores and the Triage and
Adjudicator outputs. Cite the file, not a retyped number.

## Threshold choice

The floor 0.35 was chosen on the calibration set (`omen calibrate`), before this evaluation
existed. On those 12 chunks: all 4 true matches survive and 2 of 4 hard negatives do; the weakest
true match scores 0.360 and the strongest rejected hard negative 0.343, a margin of 0.017. The
sweep is in `docs/PRODUCTION_PLAN.md` (Phase 0). The floor is **not** re-tuned on held-out data. If
held-out retrieval recall is poor, the fix named by `PLAN.md` is better `surface_forms` in
`incidents.yaml`, not a lower floor, and that change is a new tuning step that must be validated on
a fresh held-out set.

## Limits (read before quoting a number)

- **Small samples.** 8 cases per class. One case moves a rate by 12.5 points, and an exact 95% interval
  on a rate of 7/8 spans roughly 47% to 100%. Treat the numbers as a direction, not a benchmark.
- **One author, one ledger.** Six incidents, Python only, fixtures written by the people who wrote
  the incidents. No real-world false-alarm rate is measured.
- **Triage's verdict names no incident.** A `MATCH` on a true match is not checked against the
  target until the full stage, whose schema carries `incident_ref`.
- **Run-to-run variation.** Temperature is 0, but both runners go through Ollama; two runs can still
  differ. The determinism test covers one case, not this set.
- **Prompts are part of the result.** The JSON records the prompt hashes. A result is comparable
  only with results that record the same hashes.
- **Hardware.** CPU and GPU runs use different profiles (`think`, timeouts); results from the two
  are reported in separate rows.

## Results

**To be filled by the final live run on the GPU machine.** Not yet run on this branch. The Triage
prompt in force when this file was written has SHA-256
`12a45e149ff572de30813909da523be1dc5b78ee9433ec8fd7a11510c1930b88`; it has not been run against any
fixture yet. To fill the table:

```
omen seed incidents.yaml && omen reindex
omen eval --runner adk    --profile gpu --full
omen eval --runner direct --profile gpu --full
omen eval --runner adk    --profile cpu --full      # optional, on the CPU machine
```

Then copy the numbers from the printed table, and cite the `eval/results-<date>.json` file each row
came from.

| Runner | Profile | Stage | true_match found | hard_negative flagged | unrelated flagged | TP | FP | FN | TN | Precision | Recall | Source file |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| adk | gpu | retrieval | _pending_ | _pending_ | _pending_ | | | | | | | |
| adk | gpu | triage | _pending_ | _pending_ | _pending_ | | | | | | | |
| adk | gpu | full | _pending_ | _pending_ | _pending_ | | | | | | | |
| direct | gpu | retrieval | _pending_ | _pending_ | _pending_ | | | | | | | |
| direct | gpu | triage | _pending_ | _pending_ | _pending_ | | | | | | | |
| direct | gpu | full | _pending_ | _pending_ | _pending_ | | | | | | | |
| adk | cpu | retrieval | _pending_ | _pending_ | _pending_ | | | | | | | |
| adk | cpu | triage | _pending_ | _pending_ | _pending_ | | | | | | | |
| adk | cpu | full | _pending_ | _pending_ | _pending_ | | | | | | | |

Retrieval needs only the embedding model, so its rows can be filled earlier with
`omen eval --retrieval-only` on any machine that has `embeddinggemma`.
