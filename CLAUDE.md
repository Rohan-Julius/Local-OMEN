# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Omen — a fully local, scoped-down rebuild of LORE (Living Organizational
Record Engine) for a Gemma hackathon ("Agents on a Mission" track). Full
design rationale and architecture live in [PLAN.md](PLAN.md); it is the
original design, so read its "As-built status" table first, because the code
differs from it in places. The production-readiness work (branch
`production-hardening`) is planned and logged in
[docs/PRODUCTION_PLAN.md](docs/PRODUCTION_PLAN.md); a long reference for the
whole project is [docs/complete info about project.md](docs/complete%20info%20about%20project.md).
This file only summarizes what is needed day to day.

**Naming: not finalized.** "Omen" (the CLI command name, `omen`) is a
placeholder used throughout the docs and the module layout — it may change
before submission. Don't treat it as a fixed product identity. The checkout
on disk is `C:\Faizal\Local-OMEN`.

**Current state:** every phase of PLAN.md's build (0 through 9) is built, and
production-hardening phases 0 through 8 are done on the `production-hardening`
branch (security and correctness fixes, packaging and CI, trustworthy verdicts,
memory integrity, reliability, honest evaluation, usable outputs, docs). What
exists:

- `omen scan` runs Scout -> Librarian -> Triage -> Investigator -> Adjudicator
  end to end, persists every run to SQLite (`runs`, `findings`, `tool_calls`,
  `skipped_chunks`), and reports as `text`, `markdown`, `json` or `sarif`
  (`--format`, `--output`, `--fail-on` with exit code 2).
- `omen learn <postmortem.md>` and `omen learn --from-git <range>` form memories
  (`learned_by='archivist:postmortem'` / `'archivist:git'`); learn runs and their
  tool calls are persisted too. The git path runs the deterministic `sifter.py`
  prefilter first (no LLM).
- `omen memory list|show|history|forget|revert`, `omen doctor [--fix]`, `omen seed`,
  `omen reindex`, `omen calibrate` (tuning set), `omen eval` (held-out set).
- `demo_repo/` is a small example service: a planted true positive
  (`permissions.py`, matches the seeded `OMEN-001`), a hard negative
  (`cache/response_cache.py`) and ordinary files. Its own nested git history is
  gitignored and not in a clone: `python scripts/build_demo_repo.py` rebuilds a
  reconstruction (seven commits: one real fix and keyword-avoiding decoys) so
  `omen learn --from-git HEAD --repo demo_repo` can be tried.
- Not built: the Chronicler persona, `--fast`, `--no-persona`, live streaming of the
  models' reasoning (only tool calls stream). See PLAN.md "As-built status".

`README.md` is the user-facing README; this file and `tests/` are the source of
truth for what is implemented and verified. **Live results on this branch come
from a CPU run (phase 5) and are pending a final GPU run**; the list of what is
pending is in docs/PRODUCTION_PLAN.md ("Pending live verification").

## Environment

- Dev and demo are the **same machine**: Windows, RTX 5050 Laptop (8GB VRAM,
  Blackwell sm_120). There is no separate Mac dev tier despite what PLAN.md's
  "Decisions locked" table assumes — build directly against the 12B model.
  (Some production-hardening work was done on a CPU-only laptop; use
  `--profile cpu` there.)
- Python venv at `.venv` (Python 3.11.9 — 3.12 isn't installed on this
  machine and 3.14 is too new for some ML wheels). Activate before running
  anything: `.venv\Scripts\activate` (PowerShell) or
  `./.venv/Scripts/python.exe` directly. The package is installable:
  `pip install -e ".[dev]"` gives an `omen` command that works from any directory.
- Dependencies are pinned in `pyproject.toml` (and mirrored in
  `requirements.txt`): `google-adk==2.5.0`, `google-genai==2.34.0`,
  `litellm==1.94.0`, `chromadb==1.5.9`, `pydantic==2.13.4`, `ollama==0.6.2`,
  `pyyaml==6.0.3`; dev extras `pytest==9.1.1`, `pytest-asyncio==1.4.0`,
  `pytest-cov==7.1.0`, `ruff==0.17.4`. `mypy` is optional and not set up.
- Models are pulled via Ollama already: `gemma4:12b-it-q4_K_M`,
  `embeddinggemma`. Check with `ollama list` / `ollama ps` or `omen doctor`.
- Config: `OLLAMA_HOST` (one address for every caller), `OMEN_HOME` / `--store`
  (store directory, default `./omen_store`), `OMEN_PROFILE` / `--profile`
  (`gpu`, `cpu`, `auto`). Never read or print `.env` values.

### Critical Ollama/GPU quirks (see README.md for the user-facing version)

These are load-bearing for correctness and latency, not just tuning:

1. **Always pass `num_gpu=999`** when calling the model. Without it, Ollama
   silently splits the model ~30/70 CPU/GPU on this card and generation
   drops to ~5 tok/s. `num_ctx=4096` fits fully on GPU; `num_ctx=8192` does
   not.
2. **Set `think=False`** for latency-sensitive structured roles (Triage,
   Adjudicator). Gemma 4's default "thinking" mode costs ~8x latency (~56s
   vs ~7s per call) for no observed quality gain — the track's "reasoning"
   requirement is satisfied by the output schema's `reasoning` field and the
   Investigator's tool trace, not by hidden chain-of-thought.
3. **The first inference call after any Ollama restart can hard-crash** the
   CUDA backend (`llama-server process has terminated ... CUDA error:
   shared object initialization failed`) and then succeed on immediate
   retry. `omen/warmup.py` issues one throwaway generation with one retry
   before the first LLM call of `scan`, `learn` and `eval` (production plan
   Phase 5; `--no-warmup` skips it). Do it by hand before a demo too.
4. Tool-calling agents must run **non-streaming** (`RunConfig(streaming_mode=
   StreamingMode.NONE)`) — ADK reaches Ollama through LiteLLM's
   OpenAI-compatible path, which has a known bug parsing streamed
   `tool_calls`.
5. Model kwargs go through `LiteLlm(model=f"ollama_chat/{MODEL}", num_ctx=...,
   num_gpu=..., think=...)` — note the `ollama_chat/` prefix, never
   `ollama/`.
6. **`temperature` is the one kwarg that does NOT reliably reach Ollama as
   a `LiteLlm(...)` constructor kwarg** — unlike num_gpu/num_ctx/think.
   Verdicts varied run to run until temperature was set via ADK's own
   `generate_content_config=GenerateContentConfig(temperature=0)` on the
   `LlmAgent` instead. `DirectOllamaRunner` doesn't have this problem
   (temperature goes straight into `options`).

## Architecture (see PLAN.md for the original design and its as-built status)

Two local agent missions share one Gemma 4 model (loaded once, prompts
swapped against resident weights), one memory store, and one tool layer.

- **`omen learn`** (memory formation): a postmortem file or raw
  `--from-git <range>` history feeds an **Archivist** agent that
  deduplicates against existing memory (`search_memory`) before writing
  (`write_incident`, which merges into an existing incident instead of
  overwriting it). The git path uses a **Sifter** (deterministic
  prefilter, no LLM) to cut a commit range down to a handful of
  candidates, and its prompt must describe the **pre-change failure**, not
  the fix — a fix commit shows the remedy, not the bug.
- **`omen scan <path>`** (investigation): deterministic pipeline —
  **Scout** (git diff scope + `ast` chunking, pure Python) → **Librarian**
  (embed + Chroma KNN + collapse/gate, pure Python) → **Triage** (fast
  structured verdict, no tools) → **Investigator** (tools, agentic loop,
  only on Triage positives, capped by `--max-gated` / `--max-positives`) →
  **Adjudicator** (final verdict from the investigation transcript, blind to
  Triage; a `confirmed` verdict must cite `path:line` evidence the
  Investigator actually saw, or it is downgraded to `unverified`) → **Scribe**
  (persist + report). The Chronicler (persona) is not built.

Stage *order* is deterministic Python; agency is spent *within* a stage
(e.g., the Investigator decides which tools to call and when to stop).

### The four chokepoint modules

Each isolates one reversible risk — a change of mind here should never
ripple elsewhere:

| Module | Owns | Escape hatch |
|---|---|---|
| `omen/config.py` | which Gemma model is named; the one Ollama address | swap model in one line |
| `omen/runners.py` | `RoleRunner` protocol: ADK vs. direct-Ollama execution | `--runner=direct` bypasses ADK entirely |
| `omen/agents.py` | the only module that imports `google.adk` | nothing else may import it |
| `omen/vectors.py` | the only module that imports `chromadb`, owns distance→similarity conversion | no unhardened client, no raw distances downstream |

Commands that run no model never import ADK (`cli._make_runner` imports
`runners` lazily).

### Data model

SQLite (`omen/store.py`) is the **source of truth**, with a versioned schema
(`meta.schema_version`, ordered migrations): `incidents`, `surface_forms`,
`incident_history` (every create/update/forget/revert, so changes are
undoable), `chunk_vectors` (hash-cache, exact-key only), `runs`, `findings`,
`tool_calls` (the audit trail), `skipped_chunks`. Chroma (`omen/vectors.py`)
is a **derived index**, rebuildable any time via `omen reindex` (or
`omen doctor --fix`) — never write to it directly. Chroma must be
constructed with `embedding_function=None` and
`Settings(anonymized_telemetry=False)` (plus `ANONYMIZED_TELEMETRY=FALSE`)
or it will silently try to download a default embedding model and phone
home telemetry.

Constraint that matters when writing agent code: **no ADK agent may have
both `tools` and `output_schema`** — split into a tools-agent
(Investigator) feeding a separate schema-agent (Adjudicator) instead.
(Nothing asserts this; only the structure of `agents.py` keeps them apart.)

### Rules for prompts, fixtures and thresholds

Prompts are `omen/prompts/*.txt`, and tool docstrings and the CLI's generated
prompt text count as prompts. Do not edit any of them, `incidents.yaml`, the
embedding prefixes or `fixtures/` without re-running `omen calibrate` and the
fixture/Triage gates, and log before/after numbers (docs/PRODUCTION_PLAN.md,
ground rule 3). `fixtures/calibration.yaml` is the tuning set;
`fixtures/heldout.yaml` is never used for tuning and is only measured with
`omen eval`. No fixture may appear in a prompt (a test enforces it).

## Commands

CLI entry point is `omen/cli.py` (`entrypoint()` -> `asyncio.run(main())`;
ADK is async throughout). After `pip install -e ".[dev]"` the command is
`omen`; `python -m omen.cli` also works.

```
omen seed incidents.yaml          # load the starter ledger (then: omen reindex)
omen reindex
omen doctor [--fix]
omen memory list | show REF | history REF | revert REF [--to N] | forget REF
omen learn postmortems/<file>.md  |  omen learn --from-git HEAD --repo demo_repo [--dry-run]
omen scan <path> [--since REV] [--all] [--format text|markdown|json|sarif] [--output FILE] [--fail-on confirmed|unverified]
omen scan <path> --dry-run | --retrieval-only     # no generation
omen calibrate [--fixtures-path ...]              # tuning set
omen eval [--full] [--retrieval-only] [--runner adk|direct]   # held-out set -> eval/results-<date>.json
```

Exit codes: 0 success, 1 user error, 2 `--fail-on` tripped, 3 runtime or model error.
`calibrate`, `seed` and `eval` take explicit paths when run outside the checkout.

### Tests

Two tiers (`pytest.ini` registers `live` and `slow`; `tests/conftest.py` skips
`live` tests one at a time when Ollama is unreachable):

```
ruff check .
pytest -m "not live"              # unit tier: 425 passed, 30 live deselected, 96% coverage; no model, runs in CI
pytest -m "live and not slow"     # quick live tier (needs Ollama)
pytest -m live                    # full live tier (~2.5 h on CPU with OMEN_PROFILE=cpu; far less on the GPU)
pytest tests/test_scout.py        # one file
```

Most prompt-quality checks need the live model, since quality cannot be
verified any other way; unit tests use scripted fake runners and fake
embedders. CI (`.github/workflows/ci.yml`) runs `ruff check .` and the unit
tier on ubuntu and windows.
