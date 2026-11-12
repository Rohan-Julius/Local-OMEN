# Contributing

Read `CLAUDE.md` (the working summary of the code) and `docs/PRODUCTION_PLAN.md` (ground rules and the
progress log) first. `PLAN.md` is the original design; its "As-built status" table lists where the code
differs.

## Setup

```
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

On Windows install into a short path (see the README's troubleshooting section).

## Test tiers

| Tier | Command | Needs | Runs in CI |
|---|---|---|---|
| lint | `ruff check .` | nothing | yes |
| unit | `pytest -m "not live"` | nothing (no model, no Ollama) | yes, on Ubuntu and Windows |
| quick live | `pytest -m "live and not slow"` | Ollama with both models | no |
| full live | `pytest -m live` | Ollama; hours on CPU (`OMEN_PROFILE=cpu`) | no |

- Every change must keep `ruff check .` and `pytest -m "not live"` green.
- **Every bug fix gets a test that fails on the old code and passes on the new.** Look at the test fail
  first.
- A test that needs the model is marked `@pytest.mark.live` (and `@pytest.mark.slow` if it takes more than
  a few minutes). `tests/conftest.py` skips `live` tests when Ollama is unreachable. Never reach Ollama from
  an unmarked test; the unit tier blocks it. Use the scripted fake runners and fake embedders
  (`tests/test_scan_audit.py`, `tests/test_evaluation.py`) for logic that surrounds the model.
- Tests must not touch your real `omen_store/`: a fixture points the default store at a temporary
  directory. Keep it that way.
- Run the live tier on the GPU machine before a release, and record the result in the progress log.

## The calibration rule

Retrieval quality depends on `incidents.yaml`, the embedding prefixes, the prompts and the similarity
threshold, and a change to any of them can shift results without any code failing. Therefore:

1. **Prompts, tool docstrings and generated prompt text count as prompts**, because the model sees them.
   Do not edit them, `incidents.yaml`, the embedding prefixes or `fixtures/`, unless that is the point of
   your change.
2. If you do: run `omen calibrate`, then `tests/test_fixtures.py` and `tests/test_triage.py`, and write the
   **before and after numbers** (and the SHA-256 and diff of any changed prompt) in the progress log. A
   change that lifts recall while wrecking precision must fail here, not in front of a user.
3. **`fixtures/calibration.yaml` is the tuning set; `fixtures/heldout.yaml` is never used for tuning.** Do
   not edit a prompt, a surface form or the threshold to make a held-out result look better. Measure the
   held-out set with `omen eval` and report whatever it says. If you tune, write new held-out cases first.
4. **No fixture may appear in a prompt** (a test enforces it). A fixture used as a worked example is no
   longer a test.
5. If a true match fails to retrieve, fix `incidents.yaml`'s surface forms; do not lower the threshold.

## Commit and pull-request policy

- **Low commit frequency:** one commit per logical phase or change set, message `phase N: <summary>` on the
  hardening branch, or a short imperative summary elsewhere. Do not commit work in progress.
- Commit as yourself; check `git log -1 --format=%an` before pushing.
- Do not commit secrets or `.env`. Do not commit `omen_store/` (ignored).
- **One pull request per branch, opened only after the team has discussed it.** Describe the change
  phase by phase, with test counts, open items, and anything not verified live
  (`docs/PR_DESCRIPTION.md` is the model).
- Keep the architecture: a deterministic pipeline, Gemma through Ollama, SQLite as the source of truth with
  Chroma as a derived index, both runners. Fix and harden; do not redesign without discussion.
- `mypy` is optional and not set up.
