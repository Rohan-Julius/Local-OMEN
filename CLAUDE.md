# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Omen — a fully local, scoped-down rebuild of LORE (Living Organizational
Record Engine) for a Gemma hackathon ("Agents on a Mission" track). Full
design rationale, architecture, and the 8-hour build plan live in
[PLAN.md](PLAN.md) — read it before making architectural changes; this file
only summarizes what's needed to be productive day-to-day.

**Naming: not finalized.** "Omen" (the CLI command name, `omen`) is a
placeholder used throughout PLAN.md, README.md, and the module layout — it
may change before submission. Don't treat it as a fixed product identity.
The working directory on disk is still `Local-lore` (unrenamed — renaming an
open project directory has its own risk and wasn't part of the code/docs
rename).

**Current state:** Phases 0-6 done (env verified; contracts/store/vectors;
Scout; Librarian + `--retrieval-only`; fixtures/calibration GO/NO-GO passed;
tools.py + ToolBudget; Triage + Investigator via both ADKRoleRunner and
DirectOllamaRunner, wired into `omen scan`'s real pipeline — see README.md
for details and acceptance results per phase). `archivist.py`, `sifter.py`,
`report.py` and the Adjudicator (Phase 7 on) are still empty/unbuilt — don't
assume those do something without checking. `omen scan` currently runs
Scout -> Librarian -> Triage -> Investigator and prints results; there is no
Adjudicator yet, so it doesn't persist findings or render a final
confirmed/rejected verdict.

## Environment

- Dev and demo are the **same machine**: Windows, RTX 5050 Laptop (8GB VRAM,
  Blackwell sm_120). There is no separate Mac dev tier despite what PLAN.md's
  "Decisions locked" table assumes — build directly against the 12B model.
- Python venv at `.venv` (Python 3.11.9 — 3.12 isn't installed on this
  machine and 3.14 is too new for some ML wheels). Activate before running
  anything: `.venv\Scripts\activate` (PowerShell) or
  `./.venv/Scripts/python.exe` directly.
- Dependencies pinned in `requirements.txt`: `google-adk==2.5.0`,
  `litellm==1.94.0`, `chromadb==1.5.9`, `pydantic==2.13.4`, `ollama==0.6.2`,
  `pyyaml==6.0.3`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`.
- Models are pulled via Ollama already: `gemma4:12b-it-q4_K_M`,
  `embeddinggemma`. Check with `ollama list` / `ollama ps`.

### Critical Ollama/GPU quirks (see README.md for full detail)

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
   retry. Always issue a throwaway warm-up call with one retry at startup
   and before any demo.
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

## Architecture (per PLAN.md — target design; Phase 7 on is not yet built)

Two local agent missions share one Gemma 4 model (loaded once, prompts
swapped against resident weights), one memory store, and one tool layer.

- **`omen learn`** (memory formation): a postmortem file or raw
  `--from-git <range>` history feeds an **Archivist** agent that
  deduplicates against existing memory (`search_memory`) before writing
  (`write_incident`). The git path uses a **Sifter** (deterministic
  prefilter, no LLM by default) to cut a commit range down to a handful of
  candidates, and its prompt must describe the **pre-change failure**, not
  the fix — a fix commit shows the remedy, not the bug.
- **`omen scan <path>`** (investigation): deterministic pipeline —
  **Scout** (git diff scope + `ast` chunking, pure Python) → **Librarian**
  (embed + Chroma KNN + collapse/gate, pure Python) → **Triage** (fast
  structured verdict, no tools) → **Investigator** (tools, agentic loop,
  only on Triage positives) → **Adjudicator** (final verdict from the
  investigation transcript, blind to Triage's reasoning) → **Chronicler**
  (persona, stretch goal) → **Scribe** (persist + report).

Stage *order* is deterministic Python; agency is spent *within* a stage
(e.g., the Investigator decides which tools to call and when to stop).

### The four chokepoint modules

Each isolates one reversible risk — a change of mind here should never
ripple elsewhere:

| Module | Owns | Escape hatch |
|---|---|---|
| `omen/config.py` | which Gemma model is named | swap model in one line |
| `omen/runners.py` | `RoleRunner` protocol: ADK vs. direct-Ollama execution | `--runner=direct` bypasses ADK entirely |
| `omen/agents.py` | the only module that imports `google.adk` | nothing else may import it |
| `omen/vectors.py` | the only module that imports `chromadb`, owns distance→similarity conversion | no unhardened client, no raw distances downstream |

### Data model

SQLite (`omen/store.py`) is the **source of truth** — `incidents`,
`surface_forms`, `chunk_vectors` (hash-cache, exact-key only), `runs`,
`findings`, `tool_calls` (the audit trail). Chroma (`omen/vectors.py`) is a
**derived index**, rebuildable any time via `omen reindex` — never write to
it directly. Chroma must be constructed with `embedding_function=None` and
`Settings(anonymized_telemetry=False)` (plus `ANONYMIZED_TELEMETRY=FALSE`)
or it will silently try to download a default embedding model and phone
home telemetry.

Constraint that matters when writing agent code: **no ADK agent may have
both `tools` and `output_schema`** — split into a tools-agent
(Investigator) feeding a separate schema-agent (Adjudicator) instead.

## Commands

CLI entry point is `omen/cli.py` (`asyncio.run(main())`, per PLAN.md — ADK
is async throughout). Working today: `seed`, `reindex`, `memory list`,
`memory forget`, `calibrate`, and `scan` (`--dry-run` Scout-only,
`--retrieval-only` +Librarian, or the full Triage+Investigator pipeline;
`--runner={adk,direct}` picks the RoleRunner). `omen learn` (Archivist)
lands in Phase 8.

```
.venv\Scripts\activate
python -m omen.cli seed incidents.yaml
python -m omen.cli reindex
python -m omen.cli scan <path>
```

63 tests pass across `tests/` (some skip gracefully without a reachable
Ollama — most of the suite needs live inference, not mocks, since prompt
quality can't be verified any other way):

```
pytest                          # full suite (~5-6 min; most tests hit the live model)
pytest tests/test_scout.py      # single test file, fast, no model needed
pytest tests/test_triage.py     # the fixture precision/recall gate
```
