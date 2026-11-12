# Omen

[![CI](https://github.com/mohamed-faizal007/Local-OMEN/actions/workflows/ci.yml/badge.svg?branch=production-hardening)](https://github.com/mohamed-faizal007/Local-OMEN/actions/workflows/ci.yml)

Omen is a local, agentic system that turns your team's own incident history
into a standing set of code reviewers. You teach it about past failures,
either by pointing it at a postmortem or by letting it read straight from git
history, and it remembers the *mechanism* behind each one, not just the file or
the fix. Point it at a codebase afterward and it flags new code that is quietly
set up to fail the same way, with cited evidence for why.

Two commands, one idea:

- `omen learn <postmortem.md>` / `omen learn --from-git <range>` turn a written
  postmortem, or raw commit history, into a durable, technology-agnostic memory
  of a failure.
- `omen scan <path>` checks a codebase (or just a diff) against every memory
  learned so far, and explains, with mechanism, reasoning and evidence, why a
  chunk of code repeats one.

Everything runs on your machine against a Gemma model served by
[Ollama](https://ollama.com/). Nothing is sent to a third-party API.

> **Status.** All planned features of the scan and learn missions work, and the
> branch has been hardened for production use (see `CHANGELOG.md`). Live results
> on this branch come from a **CPU run**; a final run on the GPU machine is
> still pending (`docs/PRODUCTION_PLAN.md`, "Pending live verification"). Python
> only; one Gemma model; no web UI.

## Why a local model

**Privacy.** Omen reads your source code and the postmortems that describe how
something went wrong internally: proprietary logic, security-sensitive paths,
detailed outage write-ups. Running Gemma through Ollama means none of it leaves
the machine: no hosted API, no vendor logging, no retention policy to trust.

**Cost.** A scan can call the model several times per code chunk (a fast
structured pass, a tool-calling investigation, a final adjudication). Against a
per-token API that gets expensive quickly; against a local model every call is
free once the weights are downloaded, so the pipeline can afford to be thorough.

## Install

You need Python 3.11 or 3.12, [Ollama](https://ollama.com/) installed and
running, and about 8 GB of GPU memory for the 12B model (or patience: see
[CPU and GPU profiles](#cpu-and-gpu-profiles)).

```
git clone <this-repo> Local-OMEN
cd Local-OMEN

py -3.11 -m venv .venv          # Windows; elsewhere: python3.11 -m venv .venv
.venv\Scripts\activate          # elsewhere: source .venv/bin/activate
pip install -e .                # add ".[dev]" for tests and the linter

ollama pull gemma4:12b-it-q4_K_M
ollama pull embeddinggemma
```

`pip install -e .` gives an `omen` command that works from any directory.
`python -m omen.cli ...` is equivalent. Running Windows? Install into a
**short path** (see [Troubleshooting](#troubleshooting)).

Check the setup:

```
omen doctor
```

(After step 1 of the quick start below, `doctor` reports on your ledger, your
vector index, Ollama and both models.)

## Quick start

From the repository root:

```
omen seed incidents.yaml        # load the six starter incidents into the ledger
omen reindex                    # build the vector index from the ledger
omen memory list                # OMEN-001 ... OMEN-006

omen scan demo_repo --all       # scan the bundled example service
```

The demo scan should confirm `permissions.py` as a repeat of `OMEN-001` (a cached
permission check with no invalidation on revoke) and not flag
`cache/response_cache.py`, a real cache that has nothing to do with authorization.

Teach it something new:

```
omen learn postmortems/2025-08-14-cross-request-user-leak.md

python scripts/build_demo_repo.py                       # once: recreate demo_repo's git history
omen learn --from-git HEAD --repo demo_repo --dry-run   # drop --dry-run to actually learn
```

`demo_repo/` is a small example service with a planted true positive and a hard
negative. Its own git history is not in a clone (it is a nested repository that
the main repository ignores). `scripts/build_demo_repo.py` rebuilds a seven-commit
reconstruction: one real fix and decoy commits for the prefilter to reject. It
never touches an existing `demo_repo/.git`.

Scan only what you changed, and get a report a machine can read:

```
omen scan --since HEAD --format sarif --output omen.sarif --fail-on confirmed
```

## CPU and GPU profiles

Omen picks limits and `think` settings from a **hardware profile**:

| | `gpu` | `cpu` |
|---|---|---|
| Typical structured call | about 7 s (RTX 5050) | 110 to 170 s (i7 laptop) |
| One model generation may take | 120 s | 600 s |
| One investigation may take | 90 s | 1800 s |
| Time spent inside tools | 60 s | 120 s |
| Investigator and Archivist `think` | on | off |

Choose with `--profile gpu|cpu|auto` or the `OMEN_PROFILE` environment variable.
`auto` (the default) asks Ollama whether the model is on the GPU, after a warm-up
call; if it cannot tell, it assumes `gpu` and says so. **On a CPU-only machine,
always pass `--profile cpu`** (or set `OMEN_PROFILE=cpu`): with the GPU limits a
slow model turn can use up an investigation's time.

Two settings matter on a GPU. Omen always passes `num_gpu=999`, because without
it Ollama silently splits the model across CPU and GPU and throughput collapses
(about 5 tokens/s). It uses `num_ctx=4096`, which fits fully on an 8 GB card;
8192 does not. The first model call after Ollama starts can crash the CUDA
backend and then succeed on retry, so every command that uses the model makes a
throwaway warm-up call first (with one retry; skip it with `--no-warmup`). All of
these live in `omen/config.py` and `omen/profiles.py`.

A CPU scan of the demo repo took about 28 minutes in the last measured run; a
GPU scan is aimed at about two minutes (`PLAN.md`; not measured on this branch).

## Commands

All commands accept `--store DIR` (default: `$OMEN_HOME`, else `./omen_store`),
`--profile`, `-v/--verbose` and `-q/--quiet`.

| Command | What it does |
|---|---|
| `omen seed <incidents.yaml>` | Load a YAML file of incidents into the ledger (replaces an incident with the same ref). Run `omen reindex` after. |
| `omen reindex` | Rebuild the Chroma vector index from SQLite (the full rebuild; writes during `learn` update only the changed incident). |
| `omen doctor [--fix]` | Check the store: does the index match the ledger (missing, stale or extra entries), is Ollama reachable, are both models pulled, schema version, ledger size. `--fix` rebuilds a drifted index. Exit 0 healthy, 1 problems. |
| `omen memory list` | List incidents with their ref and how they were learned. |
| `omen memory show <ref>` | One incident in full: mechanism, rule, surface forms, every source it has had. |
| `omen memory history <ref>` | Its create/update/forget/revert history. |
| `omen memory revert <ref> [--to N]` | Undo its last change, or bring back a forgotten incident. |
| `omen memory forget <ref>` | Remove an incident from the ledger and the index. Its history is kept, so `revert` restores it. |
| `omen learn <postmortem.md>` | Form a memory from a postmortem, or merge into a near-duplicate. `--allow-dir DIR` widens the folder the model may read (default: the postmortem's own folder). |
| `omen learn --from-git <range>` | Form memories from raw git history (e.g. `HEAD~40..HEAD`). A deterministic prefilter (message patterns, reverts, sensitive paths) picks candidates first. `--repo`, `--max-commits`, `--max-learn`, `--dry-run`. |
| `omen scan [path]` | Run the full pipeline. `--since REV` (diff against a ref), `--all` (whole tree), `--dry-run` (chunk table only), `--retrieval-only` (ranked candidates, no generation), `--max-gated N` (default 8), `--max-positives N` (default 2), `--runner adk\|direct`, and the report options below. |
| `omen calibrate` | Sweep the similarity floor over `fixtures/calibration.yaml`. A tuning tool. |
| `omen eval` | Measure retrieval, Triage and (with `--full`) the whole pipeline on the held-out `fixtures/heldout.yaml`; prints precision and recall and writes `eval/results-<date>.json`. See `docs/EVALUATION.md`. |

**Outside the checkout.** `omen seed`, `omen calibrate` and `omen eval` read files
you name, and the repository's `incidents.yaml` and `fixtures/` are not installed
with the package. Run from a clone, or pass the paths: `omen seed
/path/to/incidents.yaml`, `omen calibrate --fixtures-path /path/to/calibration.yaml`,
`omen eval --fixtures-path /path/to/heldout.yaml`. Prompts are packaged and need no path.

### How they fit together

`learn` and `seed` both write to the ledger. `learn` re-embeds only the incident
it changed, so a freshly learned incident is found by the very next `scan`. `seed`
loads a curated file; `reindex` and `doctor --fix` repair the index; `memory`
lets you inspect and undo what is stored. `calibrate` and `eval` are measurement
tools and never change the ledger.

## Outputs and exit codes

`omen scan --format text|markdown|json|sarif [--output FILE] [--fail-on confirmed|unverified]`

| Format | For |
|---|---|
| `text` (default) | A person at a terminal: live progress, then a summary |
| `markdown` | A pull-request comment or wiki page |
| `json` | Scripts (entries are exactly the `Finding` model) |
| `sarif` | GitHub code scanning and other SARIF 2.1.0 viewers (a rule per incident, a result per confirmed or unverified finding, evidence in the message) |

For everything except `text`, stdout carries only the document; progress goes to
stderr. Details, the pre-commit hook and an example workflow for a self-hosted GPU
runner are in [`docs/OUTPUTS.md`](docs/OUTPUTS.md).

| Exit code | Meaning |
|---|---|
| 0 | Success (with `--fail-on`: nothing at or above the level) |
| 1 | User error: bad arguments, a missing file, an unknown ref, an empty ledger |
| 2 | `--fail-on` found a finding at or above the level |
| 3 | Runtime or model error: Ollama down, a model not pulled, a timeout |

Pre-commit, in `.pre-commit-config.yaml` (needs a running Ollama and a seeded ledger):

```yaml
repos:
  - repo: https://github.com/<org>/Local-OMEN
    rev: <tag-or-commit>
    hooks:
      - id: omen-scan          # runs: omen scan --since HEAD --fail-on confirmed
```

Every run is also recorded in SQLite (`runs`, `findings`, `tool_calls`,
`skipped_chunks`), including the tool-call audit trail of each investigation.

## Agent architecture

Omen runs one Gemma model, loaded once, with different prompts swapped in per
role. The *order* of the stages is fixed, ordinary Python; each role is free to
reason, and two of them to call tools, within its own turn.

### Learning a memory (`omen learn`)

1. **Sifter** *(git input only)*: a deterministic, no-LLM prefilter. It flags
   commits by message pattern (`fix`, `revert`, `regression`, ...), by revert
   detection and by touches to sensitive paths (auth, cache, payment,
   permission), cutting a range to a handful of candidates before any model call.
2. **Archivist**: reads the postmortem or a candidate commit's diff and forms the
   memory. Its central discipline is **abstraction**: it records the failure as a
   technology-agnostic *mechanism*, not as the specific library involved. For git
   input it reconstructs the failure from the code *before* the fix, since a fix
   commit shows the remedy, not the bug. Before writing it searches memory for a
   near-duplicate and, if it finds one, **merges** into it: existing surface forms
   are kept, new ones added, the original provenance kept and the new source
   appended. Every change is recorded in the incident's history.

### Scanning a codebase (`omen scan`)

1. **Scout** (pure Python): resolves scope (uncommitted changes including staged
   and untracked files, `--since REV`, or `--all`) and chunks files by AST into
   function-sized pieces.
2. **Librarian** (pure Python plus the embedding model): embeds chunks, queries
   the index in one batch, collapses hits to one candidate per incident and drops
   anything below the calibrated similarity floor (0.35).
3. **Triage**: a fast, tool-free structured verdict (`MATCH` / `NO_MATCH`) on
   whether the chunk's own failure mechanism matches a candidate, not just its
   vocabulary. The default is `NO_MATCH`.
4. **Investigator**: runs only on Triage positives (at most `--max-positives`).
   It has tools (`read_code`, `grep_symbol`, `search_memory`, `get_incident`) and
   gathers evidence; it does not rule on the match.
5. **Adjudicator**: the final verdict, `confirmed`, `unverified` or `rejected`,
   from the chunk, the candidates and the investigation transcript only. It never
   sees Triage's reasoning. In code, a `confirmed` verdict must cite `path:line`
   evidence that lies in the chunk or in lines the Investigator actually read, and
   the Investigator must have made at least one successful tool call; otherwise it
   is downgraded to `unverified` with the reason recorded.

Chunks that clear retrieval but exceed a cap are reported and stored, never
silently dropped.

### Two execution backends

Every role runs through a `RoleRunner` interface with two implementations,
chosen with `--runner adk|direct`: `ADKRoleRunner` (Google's Agent Development
Kit, the default) and `DirectOllamaRunner` (talks to Ollama directly; a fallback if
ADK's structured-output or tool path misbehaves). Both are exercised by the live
runner contract tests (`tests/test_runners.py`) and by the Adjudicator's structured
call; most other live tests use ADK only, and the unit tests use scripted fake
runners for both.

### Data model

SQLite is the source of truth: incidents with their abstracted mechanism and
surface forms, an incident history, a hash cache of chunk embeddings, run records,
findings and the tool-call audit trail. The schema is versioned and migrated on
open. Chroma holds only a derived vector index; it is rebuilt from SQLite by
`omen reindex` and can be deleted and regenerated at any time.

## Testing and evaluation

```
ruff check .
pytest -m "not live"              # unit tier: no model needed, runs in CI
pytest -m "live and not slow"     # quick live tier (needs Ollama)
pytest -m live                    # full live tier
```

The unit tests use scripted fake runners and fake embedders. Prompt quality can
only be checked against the real model, so the live tier exists and is part of the
release checklist (`CONTRIBUTING.md`). Detection quality is measured on a held-out
fixture set that is never used for tuning: see [`docs/EVALUATION.md`](docs/EVALUATION.md)
(its results table is still to be filled by a GPU run).

## Troubleshooting

**Install fails on Windows with a "path too long" error** (typically on a data
file inside `litellm`). Windows limits paths to 260 characters unless long paths
are enabled, and Omen's dependencies have deep trees. Either create the virtual
environment at a short path (for example `C:\omen\.venv`), or enable long paths
(Windows 10 and later: `LongPathsEnabled` = 1 under
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`, plus
`git config --system core.longpaths true`) and install again.

**`omen seed`, `calibrate` or `eval` says a file is not found.** They take explicit
paths when you run outside the checkout; see
[Commands](#commands). `omen scan` and `omen memory` use `--store` / `OMEN_HOME` (default
`./omen_store` in the **current** directory, so run from the same place each time or
set `OMEN_HOME`).

**`omen doctor` says the index has drifted** (missing, stale or extra entries):
the vector index no longer matches the ledger, for example after editing the
database by hand. `omen doctor --fix` rebuilds the index from the ledger (it needs
Ollama for the embeddings). Exit code 0 means healthy; 1 means a problem remains
(drift, Ollama unreachable or a model not pulled).

**`error: Ollama is not reachable at ...`** (exit 3): start it with `ollama serve`,
or set `OLLAMA_HOST` if it runs elsewhere (one address is used for every call).

**`error: model ... not found; run ollama pull ...`** (exit 3): pull the named model.

**Everything is very slow, or `llm_timeout` / "timed out" appears.** You are
probably on a CPU. Use `--profile cpu` (or `OMEN_PROFILE=cpu`), expect minutes per
model call, and consider `--max-gated` and `--max-positives` to bound a scan.

**The first call fails with "CUDA error" or "llama-server process has
terminated".** A known first-call crash after an Ollama restart. Omen's warm-up
retries once; if it still fails, run any `ollama run` once and try again.

**`error: the incident ledger is empty`** (exit 1): run `omen seed incidents.yaml`
then `omen reindex`.

**A scan reports "skipped N chunk(s) because of caps".** Chunks beyond
`--max-gated` were not triaged and Triage positives beyond `--max-positives` were
not investigated; they are listed and stored in `skipped_chunks`. Raise the caps to
process them.

## Documentation

| File | What |
|---|---|
| `CHANGELOG.md` | What changed on this branch, phase by phase |
| `docs/OUTPUTS.md` | Report formats, exit codes, pre-commit, CI example |
| `docs/EVALUATION.md` | How detection quality is measured, and the results table |
| `docs/PRODUCTION_PLAN.md` | The hardening plan, the progress log and the pending live checks |
| `docs/complete info about project.md` | A long reference for the whole project |
| `PLAN.md` | The original design (see its "As-built status") |
| `SECURITY.md`, `CONTRIBUTING.md` | Threat model and reporting; tests, calibration rule, commit policy |
