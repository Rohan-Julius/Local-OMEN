# Omen

[![CI](https://github.com/mohamed-faizal007/Local-OMEN/actions/workflows/ci.yml/badge.svg?branch=production-hardening)](https://github.com/mohamed-faizal007/Local-OMEN/actions/workflows/ci.yml)

> Turn a team's own incident history into a standing set of code reviewers — an agent that
> learns the *mechanism* behind a past failure (from a postmortem, or straight from `git`
> history) and later explains, with cited evidence, why a new chunk of code repeats it. Runs
> entirely against a local Gemma model; nothing leaves the machine.

A codebase forgets. A team hits an incident, writes a postmortem, fixes it — and eighteen
months later someone reintroduces the same failure mode written a different way. Linters can't
catch it, because the knowledge isn't a generic best practice, it's *this team's specific
history*. Grep can't catch it either, because the second occurrence rarely reuses the first
one's vocabulary — a Redis-token leak the first time, an in-process `functools.lru_cache` with
the same missing invalidation the second.

Omen is built around two commands that close that loop: `omen learn` forms a durable,
technology-agnostic memory of a failure; `omen scan` checks a codebase against every memory
learned so far. Both run against a local [Gemma][gemma] model served by [Ollama][ollama] —
deliberately, not incidentally: the material being read (proprietary source code, and
postmortems that describe an outage or vulnerability in detail) is exactly what a team can't
casually ship to a third-party API, and a local model makes an otherwise cost-prohibitive
multi-stage judging pipeline free to run per call.

[gemma]: https://ai.google.dev/gemma
[ollama]: https://ollama.com/

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Usage walkthrough](#usage-walkthrough)
- [Commands](#commands)
- [Data model](#data-model)
- [Configuration](#configuration)
- [CPU and GPU profiles](#cpu-and-gpu-profiles)
- [Outputs and exit codes](#outputs-and-exit-codes)
- [Tech stack](#tech-stack)
- [Testing](#testing)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)
- [License](#license)

## Features

### Memory formation (`omen learn`)

- **Two input paths to the same memory.** `omen learn <postmortem.md>` reads a curated
  document; `omen learn --from-git <range>` reads raw commit history with no human-written
  summary at all — a deterministic Sifter prefilters the range by message pattern, revert
  detection, and sensitive-path signals before any model call happens.
- **Abstraction over description.** A learned incident records a technology-agnostic
  *mechanism* ("a permission cache has no invalidation path tied to the revocation event"),
  plus several concrete surface forms that deliberately span technologies the source incident
  never used — the single biggest lever on later retrieval quality.
- **Failure, not fix.** On the git path, the Archivist is prompted to reconstruct the
  *pre-change* state — a fix commit shows the remedy being added, not the bug that made it
  necessary.
- **Dedup before write.** Every write checks the ledger for a near-duplicate first
  (`search_memory`) and merges into that entry instead of creating a redundant one: existing surface forms are kept, new ones added.
- **A real undo.** `omen memory forget <ref>` removes an incident from both SQLite and the
  vector index, and `omen memory revert <ref>` restores it (or undoes a merge) from the incident's
  history — the safety valve that makes autonomous memory writes acceptable.

### Investigation (`omen scan`)

- **Deterministic scoping.** Defaults to the working-tree `git diff`; `--since <rev>` diffs a
  specific ref, `--all` scans the whole tracked tree. Files are chunked by `ast` into
  function/method/class-sized pieces, not fixed-size windows.
- **Retrieval before generation.** Chunks are embedded, queried against Chroma, collapsed to
  one best-variant candidate per incident, and gated below a calibrated similarity floor —
  entirely in pure Python, no model call yet.
- **Two-stage judgment, not one.** A fast, tool-free Triage pass rules `MATCH`/`NO_MATCH` on
  every gated chunk; only `MATCH` chunks proceed to the expensive, tool-driven Investigator, so
  agentic cost is spent where it can change the answer, not spent uniformly.
- **Evidence-backed final verdict.** The Adjudicator sees the chunk, the candidate incidents,
  and the Investigator's transcript — deliberately *not* Triage's reasoning or verdict, so it
  can't rubber-stamp the first stage's opinion.
- **A full audit trail.** Every tool call made during every investigation is recorded to
  SQLite (`tool_calls`), alongside per-run timing and per-finding evidence (`runs`,
  `findings`).

### Safeguards

- **Nothing self-confirms.** A `confirmed` verdict must cite `path:line` evidence the
  Investigator actually saw, after at least one successful tool call; otherwise it is
  automatically downgraded to `unverified` in code, never left to the model's discretion.
- **Bounded agent loops.** Max tool calls per chunk, a duplicate-call cache, and a wall-clock
  ceiling are enforced in the orchestrator — never merely requested in a prompt.
- **Path confinement.** `read_code` and `grep_symbol` resolve paths against the scanned repo
  root and reject anything outside it.
- **A reversibility hedge on execution itself.** Every LLM role runs through a `RoleRunner`
  interface with two interchangeable backends (`--runner={adk,direct}`) — if Google ADK's
  structured-output or tool-calling path misbehaves, direct Ollama calls are a flag away, not a
  rewrite.

## Architecture

Omen runs **one** Gemma model, loaded once, with different prompts swapped in per role — it is
not several separate models. The pipeline's stage *order* is fixed, ordinary Python; agency is
spent *within* a stage — the Investigator decides which tools to call and when to stop, and the
Archivist decides whether what it's about to write is actually new.

```mermaid
graph TD
    PM[/Postmortem file/] --> Arch1[Archivist]
    Git[/git history range/] --> Sift[Sifter: deterministic prefilter]
    Sift -->|candidate commits| Arch2[Archivist]
    Arch1 --> Dedup{search_memory:<br/>near-duplicate?}
    Arch2 --> Dedup
    Dedup -->|yes| Update[Update existing incident]
    Dedup -->|no| Write[write_incident: new ref]

    Write --> Ledger[(SQLite ledger)]
    Update --> Ledger
    Ledger --> Chroma[(Chroma vector index)]

    Code[/Codebase or diff/] --> Scout[Scout: ast chunking]
    Scout --> Lib[Librarian: embed + KNN + gate]
    Chroma -.retrieval.-> Lib
    Lib -->|gated chunks| Tri{Triage:<br/>MATCH?}
    Tri -->|NO_MATCH| Drop([discarded])
    Tri -->|MATCH| Inv[Investigator: tool loop]
    Inv --> Adj[Adjudicator: final verdict]
    Adj --> Findings[(findings + tool_calls)]

    classDef store fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef gate fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef drop fill:#fecaca,stroke:#dc2626,color:#7f1d1d
    class Ledger,Chroma,Findings store
    class Dedup,Tri gate
    class Drop drop
```

### Roles

| Role | Mission | Tools | Job |
|---|---|---|---|
| **Sifter** | learn (git input only) | — (no LLM by default) | Reduce a commit range to a handful of candidates by message pattern, revert detection, and sensitive-path signals. |
| **Archivist** | learn | `read_file`/`read_commit`+`read_diff`, `search_memory`, `write_incident` | Abstract a failure into a technology-agnostic mechanism plus surface forms; dedup before writing. |
| **Scout** | scan | — | Resolve scope via `git`; `ast`-chunk files into reasoning-sized pieces. |
| **Librarian** | scan | — | Embed chunks, query the vector index, collapse to one candidate per incident, gate below threshold. |
| **Triage** | scan | — | Fast structured `MATCH`/`NO_MATCH` ruling. Default assumption is `NO_MATCH`; it has to be argued out of. |
| **Investigator** | scan (Triage `MATCH` only) | `read_code`, `grep_symbol`, `search_memory`, `get_incident` | Agentic evidence gathering — is there a mitigation elsewhere, is this code actually reachable on the path that matters. |
| **Adjudicator** | scan | — | Final `confirmed`/`rejected`/`unverified` verdict from the investigation transcript, blind to Triage's reasoning. |

### Control flow

Order is deterministic Python end to end; only what happens *inside* a stage is agentic. This
is a deliberate position, not a shortcut — on constrained hardware every routing decision costs
a real inference, and the pipeline shape is fully known ahead of time. Autonomy is spent where
the steps are genuinely unknown in advance (which tools to call, how many, whether a memory is
actually new); determinism is kept everywhere the shape doesn't change.

### Two execution backends

Every role runs through a `RoleRunner` interface (`omen/runners.py`):

- **`ADKRoleRunner`** (default) — [Google's Agent Development Kit][adk], via
  `LiteLlm("ollama_chat/…")`.
- **`DirectOllamaRunner`** — talks to Ollama directly (`format=<schema>` for constrained
  structured output, native `tools=[...]` for tool calling).

Selectable per-command with `--runner={adk,direct}`; both are fully implemented and tested
equally, not one real path and one stub.

[adk]: https://google.github.io/adk-docs/

### The chokepoint modules

Four modules each isolate one reversible decision, so a change of mind in any of them never has
to ripple elsewhere:

| Module | Owns | Escape hatch |
|---|---|---|
| `omen/config.py` | Which Gemma model is named | Swap model in one line |
| `omen/runners.py` | `RoleRunner` protocol: ADK vs. direct-Ollama execution | `--runner=direct` bypasses ADK entirely |
| `omen/agents.py` | The only module that imports `google.adk` | Nothing else may import it |
| `omen/vectors.py` | The only module that imports `chromadb`, owns distance→similarity conversion | No unhardened client, no raw distances downstream |

## Quick start

Prerequisites: Python 3.11 or 3.12, and [Ollama](https://ollama.com/) installed and running.

```bash
git clone <this-repo> Local-OMEN
cd Local-OMEN

py -3.11 -m venv .venv
.venv\Scripts\activate            # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e .                  # gives an `omen` command that works from any directory
                                  # (use ".[dev]" for the tests and linter)

ollama pull gemma4:12b-it-q4_K_M
ollama pull embeddinggemma
```

`omen <command>` is equivalent to `omen <command>`; the examples below use `omen`.
On Windows, install into a short path (see [Troubleshooting](#troubleshooting)). Check the
setup with `omen doctor`.

If you're on a GPU-constrained card, model loading matters: Ollama can silently split the model
across CPU/GPU and tank throughput unless it's told to keep the whole model resident on GPU.
`omen/config.py` is the one place model and runtime flags (`NUM_GPU`, `NUM_CTX`,
`THINK_DEFAULT`) live — see [Configuration](#configuration). On a CPU-only machine, see
[CPU and GPU profiles](#cpu-and-gpu-profiles).

## Usage walkthrough

1. **Bootstrap the ledger.** Load the starter incident set and build its vector index:

   ```bash
   omen seed incidents.yaml
   omen reindex
   omen memory list
   ```

   You should see the seeded incidents printed with their refs (`OMEN-001`, `OMEN-002`, ...).
   `omen memory show <ref>` prints one in full.

2. **Teach it something new**, from a postmortem or from raw history:

   ```bash
   omen learn postmortems/some-incident.md
   omen learn --from-git HEAD~40..HEAD --dry-run   # preview candidates first
   ```

3. **Scan a codebase** against everything currently in memory:

   ```bash
   omen scan path/to/a/repo
   ```

   Defaults to diffing the working tree; pass a directory and `--all` to scan every tracked
   file instead. `--dry-run` stops after chunking, `--retrieval-only` stops after ranking
   candidates — both skip every model call, useful as a fast sanity check. `--format`,
   `--output` and `--fail-on` are covered in [Outputs and exit codes](#outputs-and-exit-codes).

4. **Undo a bad memory** if `learn` got something wrong:

   ```bash
   omen memory forget OMEN-007
   omen memory revert OMEN-007      # changed your mind: restore it (or undo its last change)
   ```

### Try it on the bundled demo

`demo_repo/` is a small, self-contained example service built to exercise both commands end
to end:

- **A planted true positive** (`permissions.py`): a `functools.lru_cache`'d permission check
  with no invalidation on revoke. It shares OMEN-001's failure mechanism with none of its
  vocabulary — no Redis, no TTL.
- **A hard negative** (`cache/response_cache.py`): a genuine cache, with "cache" in every
  identifier, mechanically unrelated — it caches a static response body, nothing
  authorization-related.
- **A small constructed commit history** — one real fix commit plus keyword-avoiding decoys —
  for trying `--from-git` end to end. The history lives in a nested git repo that this
  repository ignores (`demo_repo/.git/`), so a fresh clone has the files but not the history:
  run `python scripts/build_demo_repo.py` once to rebuild it (it never touches an existing
  `demo_repo/.git`).

```bash
omen scan demo_repo --all
omen learn --from-git HEAD --repo demo_repo --dry-run   # drop --dry-run to actually learn
```

## Commands

All commands go through `omen <command>` (or `python -m omen.cli <command>`). Every command accepts `--store DIR` (default `$OMEN_HOME`, else `./omen_store`), `--profile gpu|cpu|auto`, `-v` and `-q`.

| Command | What it does |
|---|---|
| `omen seed <incidents.yaml>` | Loads a YAML file of incidents into the SQLite ledger. Used once to bootstrap a starter memory set. |
| `omen reindex` | Rebuilds the Chroma vector index from SQLite. SQLite is always the source of truth; Chroma is a derived index you can throw away and rebuild any time. |
| `omen doctor [--fix]` | Checks the store: does the index match the ledger, is Ollama reachable, are both models pulled, schema version. `--fix` rebuilds a drifted index. Exit 0 healthy, 1 problems. |
| `omen memory list` | Lists every incident currently in the ledger, with its ref and how it was learned. |
| `omen memory show <ref>` | Shows one incident in full: mechanism, rule, surface forms, every source. |
| `omen memory history <ref>` | Lists an incident's create/update/forget/revert history. |
| `omen memory revert <ref> [--to N]` | Restores an incident from history: undoes its last change, or brings back a forgotten one. |
| `omen memory forget <ref>` | Deletes an incident from both SQLite and the vector index — the undo for a bad `omen learn` run (its history is kept, so `revert` can restore it). |
| `omen learn <postmortem.md>` | Reads a postmortem file and forms a new memory (or updates a near-duplicate one) from it. |
| `omen learn --from-git <range>` | Reads raw commit history (e.g. `HEAD~40..HEAD`): the Sifter narrows the range to candidates, then each is turned into a memory. `--repo <path>` picks which repo to read (default: cwd); also takes `--max-commits`, `--max-learn`, `--dry-run`. `learn <postmortem>` also takes `--allow-dir`. |
| `omen scan <path>` | Runs the full investigation pipeline and prints a verdict per flagged chunk. `--dry-run` stops after chunking; `--retrieval-only` stops after ranking candidates (no generation); `--since <rev>` diffs a specific ref; `--all` scans the whole tracked tree; `--max-gated N` / `--max-positives N` cap how many chunks are triaged / investigated; `--format`, `--output`, `--fail-on` choose the report (see [Outputs](#outputs-and-exit-codes)). |
| `omen calibrate` | Sweeps the retrieval similarity threshold over a labeled fixture set and prints where true positives stop surviving and hard negatives start getting rejected. A tuning tool for `SIMILARITY_THRESHOLD`, not part of the normal learn/scan workflow; reads `fixtures/calibration.yaml` by default. |
| `omen eval` | Measures retrieval, Triage and (with `--full`) the whole pipeline on the held-out `fixtures/heldout.yaml`, prints precision and recall, and writes `eval/results-<date>.json`. Also `--retrieval-only`, `--runner`, `--fixtures-path`. See [docs/EVALUATION.md](docs/EVALUATION.md). |

`learn`, `scan` and `eval` also take `--runner={adk,direct}` — see
[Two execution backends](#two-execution-backends).

`learn` and `seed` write to the ledger — every `write_incident` call
immediately re-embeds the changed incident in the vector index, so a freshly learned incident is
retrievable by the very next `scan` with no manual `reindex` needed. `seed`/`reindex` bootstrap
and repair that same ledger by hand; `memory` lets you inspect and undo what's in
there; `calibrate` and `eval` are measurement tools against fixture files and never change the ledger.

`omen seed`, `calibrate` and `eval` read files you name, and `incidents.yaml` and `fixtures/` are
not installed with the package. Run them from a clone, or pass explicit paths:
`omen seed /path/to/incidents.yaml`, `omen calibrate --fixtures-path /path/to/calibration.yaml`,
`omen eval --fixtures-path /path/to/heldout.yaml`.

## Data model

**SQLite (`omen/store.py`) is the source of truth.** Chroma (`omen/vectors.py`) is a *derived*
index, rebuildable at any time via `omen reindex` — nothing ever writes to it directly.

| Table | Holds |
|---|---|
| `incidents` | The abstracted `failure_mechanism`, `what_happened`, and `the_rule` for each learned incident, plus provenance (`source`, `learned_by`). |
| `surface_forms` | Concrete manifestations of an incident's mechanism, embedded separately — the single biggest retrieval lever. |
| `chunk_vectors` | A content-hash cache of computed chunk embeddings. Exact-key lookup only, never a similarity search. |
| `incident_history` | Every create/update/forget/revert of an incident, so changes are undoable. |
| `runs` | One row per `scan`/`learn` invocation: timing, counts, which runner. |
| `findings` | One row per flagged chunk: verdict, mechanism, reasoning, evidence, similarity. |
| `tool_calls` | The full audit trail of every tool call made during every investigation. |
| `skipped_chunks` | Chunks a cap (`--max-gated`, `--max-positives`) kept from being processed — never silently dropped. |

`learned_by` distinguishes a memory a human seeded (`seed`) from one the agent formed, and
which input path formed it (`archivist:postmortem` vs. `archivist:git`); `source` carries the
postmortem path or commit SHA so any learned memory is traceable back to where it came from.

Chroma holds one vector per **embedded variant** — an incident's mechanism sentence plus each
of its surface forms — so an incident's retrieval score is the max over its variants.
`hnsw:space` is set to cosine explicitly (Chroma defaults to L2), and the collection is
constructed with `embedding_function=None` and `Settings(anonymized_telemetry=False)`, or it
will silently try to download a default embedding model and phone home telemetry.

## Configuration

Everything tunable lives in `omen/config.py` — the single chokepoint for which model is named
and how the runtime talks to it.

| Setting | Default | Why |
|---|---|---|
| `LLM_MODEL` | `gemma4:12b-it-q4_K_M` | The reasoning model for every agent role. |
| `EMBED_MODEL` | `embeddinggemma` | Chunk and incident embeddings. |
| `NUM_GPU` | `999` | Forces full GPU residency. Without it, Ollama silently splits the model across CPU/GPU and generation drops to ~5 tok/s. |
| `NUM_CTX` | `4096` | Fits fully on an 8GB card; `8192` does not. |
| `TEMPERATURE` | `0` | Applied to every role — determinism, not just Triage. |
| `THINK_DEFAULT` | `False` | Off for latency-sensitive structured roles (Triage, Adjudicator): Gemma 4's "thinking" mode costs ~8x latency for no observed quality gain on these prompts. |
| `SIMILARITY_THRESHOLD` | `0.35` | The Librarian's retrieval floor, chosen by `omen calibrate` against `fixtures/calibration.yaml`. |
| `MAX_TOOL_CALLS` / `TOOL_WALL_CLOCK_SECONDS` | `6` / `90` | Hard caps on an Investigator's tool loop, enforced in code. |
| `MAX_COMMITS` / `MAX_LEARN` | `50` / `5` | Bounds on `omen learn --from-git`: how many commits the Sifter considers, and how many candidates the Archivist actually processes. |

`OLLAMA_HOST` (one address for every call), `OMEN_HOME` (store directory) and `OMEN_PROFILE` are read from the environment. Adjust `LLM_MODEL`, `NUM_GPU`, and `NUM_CTX` here for different hardware — nothing elsewhere in
the codebase names a model or a GPU flag directly.

## CPU and GPU profiles

Omen picks time limits and `think` settings from a **hardware profile**:

| | `gpu` | `cpu` |
|---|---|---|
| Typical structured call | about 7 s (RTX 5050) | 110 to 170 s (i7 laptop) |
| One model generation may take | 120 s | 600 s |
| One investigation may take | 90 s | 1800 s |
| Investigator and Archivist `think` | on | off |

Choose with `--profile gpu|cpu|auto` or `OMEN_PROFILE`. `auto` (the default) asks Ollama whether
the model is on the GPU after a warm-up call, and assumes `gpu` if it cannot tell. **On a CPU-only
machine pass `--profile cpu`.** Every command that uses the model first makes a throwaway warm-up
call with one retry (the first call after an Ollama restart can crash the CUDA backend; skip it with
`--no-warmup`). A CPU scan of the demo repo took about 28 minutes in the last measured run; GPU
timings on this branch are still to be measured.

## Outputs and exit codes

`omen scan --format text|markdown|json|sarif [--output FILE] [--fail-on confirmed|unverified]`

| Format | For |
|---|---|
| `text` (default) | A person at a terminal |
| `markdown` | A pull-request comment or wiki page |
| `json` | Scripts (entries are exactly the `Finding` model) |
| `sarif` | GitHub code scanning and other SARIF 2.1.0 viewers |

Except for `text`, stdout carries only the document and progress goes to stderr. A pre-commit hook
(`omen-scan`) and an example CI workflow are in [docs/OUTPUTS.md](docs/OUTPUTS.md).

| Exit code | Meaning |
|---|---|
| 0 | Success (with `--fail-on`: nothing at or above the level) |
| 1 | User error: bad arguments, a missing file, an unknown ref, an empty ledger |
| 2 | `--fail-on` found a finding at or above the level |
| 3 | Runtime or model error: Ollama down, a model not pulled, a timeout |

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11 |
| Agent orchestration | Google [ADK][adk] (`LlmAgent` per role) via LiteLLM, behind a `RoleRunner` abstraction with a direct-Ollama fallback |
| Local LLM runtime | Ollama, `gemma4:12b-it-q4_K_M` |
| Embeddings | Ollama, `embeddinggemma` |
| Vector store | ChromaDB, `PersistentClient`, telemetry off |
| Structured contracts | Pydantic |
| Persistence | SQLite (ledger, runs, findings, tool-call audit trail) |
| Chunking | Python `ast` (no parser dependency) |
| Testing | Pytest, pytest-asyncio |

No paid API is required anywhere in the stack.

## Testing

```bash
ruff check .
pytest -m "not live"              # unit tier: no model needed, runs in CI
pytest -m "live and not slow"     # quick live tier (needs Ollama)
pytest -m live                    # full live tier
pytest tests/test_triage.py       # the fixture precision/recall gate (live)
```

Prompt quality for a system like this can't be meaningfully verified with mocks, so the live
tier exercises the real model; those tests skip gracefully when Ollama isn't reachable. The
unit tier uses scripted fake runners and fake embedders. Detection quality is measured on a
held-out fixture set that is never used for tuning: see [docs/EVALUATION.md](docs/EVALUATION.md).

## Project structure

```
omen/
├── omen/
│   ├── cli.py          # argparse + asyncio.run; the deterministic orchestrator
│   ├── contracts.py    # pydantic models — no logic, no third-party imports
│   ├── config.py       # only place models/runtime flags are named
│   ├── agents.py       # only place google.adk is imported
│   ├── runners.py      # RoleRunner protocol + ADKRoleRunner + DirectOllamaRunner
│   ├── tools.py        # tool functions + path confinement + call caps
│   ├── store.py        # SQLite: ledger, cache, runs, findings, tool_calls
│   ├── vectors.py      # only place chromadb is imported; owns distance→similarity
│   ├── scout.py        # git scope resolution + ast chunking
│   ├── librarian.py    # embed (cached) + query + collapse + gate
│   ├── sifter.py        # deterministic commit-range prefilter for --from-git
│   ├── evidence.py      # checks that a `confirmed` verdict's evidence is real
│   ├── render.py        # markdown / json / sarif reports
│   ├── evaluation.py    # scoring for `omen eval`
│   ├── profiles.py      # gpu / cpu limits
│   ├── doctor.py        # `omen doctor`
│   ├── archivist.py     # the learn mission
│   ├── fixtures.py      # loads the fixture files for calibrate/eval/tests
│   ├── report.py        # progress lines, terminal summary
│   └── prompts/         # triage, investigator, adjudicator, archivist (per input path)
├── incidents.yaml        # the seeded starter ledger
├── postmortems/           # raw markdown for `omen learn` to consume
├── fixtures/              # calibration.yaml (tuning) and heldout.yaml (evaluation)
├── docs/                  # plans, evaluation, outputs, reference
├── scripts/               # build_demo_repo.py
├── demo_repo/             # self-contained example service used above
└── tests/
```

## Troubleshooting

**Install fails on Windows with a "path too long" error** (typically on a data file inside
`litellm`). Windows limits paths to 260 characters unless long paths are enabled. Create the
virtual environment at a short path (for example `C:\omen\.venv`), or enable long paths
(`LongPathsEnabled` = 1 under `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`, plus
`git config --system core.longpaths true`) and install again.

**`omen seed`, `calibrate` or `eval` says a file is not found.** They take explicit paths when run
outside the checkout (see [Commands](#commands)). The default store is `./omen_store` in the
*current* directory, so run from the same place each time or set `OMEN_HOME` / `--store`.

**`omen doctor` reports drift** (missing, stale or extra index entries): `omen doctor --fix`
rebuilds the index from the ledger (needs Ollama for the embeddings). Exit 0 healthy, 1 problems.

**`error: Ollama is not reachable`** or **`model ... not found`** (exit 3): start `ollama serve`
(or set `OLLAMA_HOST`), or run the `ollama pull` the message names.

**Very slow, or "timed out".** You are probably on a CPU: use `--profile cpu` and consider
`--max-gated` and `--max-positives`.

**`error: the incident ledger is empty`** (exit 1): run `omen seed incidents.yaml`, then `omen reindex`.

**"skipped N chunk(s) because of caps".** Chunks beyond `--max-gated`, or Triage positives beyond
`--max-positives`, were not processed; they are listed and stored. Raise the caps to process them.

## Documentation

- [PLAN.md](PLAN.md) — full design rationale, the original 8-hour build plan, and the track
  alignment writeup (see its "As-built status" table for where the code differs).
- [CHANGELOG.md](CHANGELOG.md) — what changed on the `production-hardening` branch, phase by phase.
- [docs/PRODUCTION_PLAN.md](docs/PRODUCTION_PLAN.md) — the hardening plan, progress log and pending live checks.
- [docs/EVALUATION.md](docs/EVALUATION.md) — how detection quality is measured, and the results table.
- [docs/OUTPUTS.md](docs/OUTPUTS.md) — report formats, exit codes, pre-commit, CI example.
- [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md) — threat model and reporting; test tiers, calibration rule, commit policy.
- This README — commands, architecture, configuration, and how to run the project day to day.

## License

MIT — see [LICENSE](LICENSE).
