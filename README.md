# Omen

Omen is a local, agentic system that turns your team's own incident history
into a standing set of code reviewers. You teach it about past failures —
either by pointing it at a postmortem or letting it read straight from git
history — and it remembers the *mechanism* behind each one, not just the
file or the fix. Point it at a codebase afterward and it flags new code that
is quietly set up to fail the same way, with cited evidence for why.

Two commands, one idea:

- `omen learn <postmortem.md>` / `omen learn --from-git <range>` — turn a
  written postmortem, or raw commit history, into a durable, technology-
  agnostic memory of a failure.
- `omen scan <path>` — check a codebase (or just a diff) against every
  memory learned so far, and explain — with mechanism, reasoning, and
  evidence — why a chunk of code repeats one.

Everything runs on-device against a local Gemma model served by
[Ollama](https://ollama.com/); nothing is sent to a third-party API.

## Why a local model

**Privacy.** The whole point of Omen is to read your code — full source
files, diffs, and the incident postmortems that describe how something went
wrong internally. That's exactly the kind of material a team can't casually
ship to a third-party API: proprietary logic, security-sensitive code paths,
and post-incident writeups that may describe an outage or a vulnerability in
detail. Running Gemma locally via Ollama means none of it ever leaves the
machine — there's no API call to a hosted model, no vendor logging, no data
retention policy to trust.

**Cost.** Omen's scan pipeline can call the model several times per code
chunk (a fast structured pass, then a multi-step tool-calling investigation,
then a final adjudication), and a `learn` run adds a further tool-calling
pass per postmortem or commit. Against a hosted per-token API, iterating on
prompts or scanning a large codebase repeatedly gets expensive fast. Against
a local model, once the weights are downloaded, every call is free — so the
pipeline can afford to be thorough (multiple judging stages, an agentic
investigation loop) in a way that would be cost-prohibitive to run per-token
in the cloud.

## Setup

Prerequisites: Python 3.11, and [Ollama](https://ollama.com/) installed and
running.

```
git clone <this-repo>
cd Local-lore

py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

ollama pull gemma4:12b-it-q4_K_M
ollama pull embeddinggemma
```

Load the starter incident ledger and build its vector index:

```
python -m omen.cli seed incidents.yaml
python -m omen.cli reindex
python -m omen.cli memory list
```

You should see the seeded incidents printed with their refs (`OMEN-001`,
`OMEN-002`, ...). From here you can teach it a new incident or scan a repo:

```
python -m omen.cli learn postmortems/some-incident.md
python -m omen.cli scan path/to/a/repo
```

`omen scan` defaults to diffing the working tree; pass a directory and
`--all` to scan every tracked file instead of just what's changed.

### Try it against the bundled example

`demo_repo/` is a small, self-contained example service (its own git repo,
nested inside this one) with a planted true positive — a
`functools.lru_cache`'d permission check with no invalidation on revoke,
sharing OMEN-001's mechanism with no shared vocabulary at all (no Redis, no
TTL) — and a hard negative: a genuine cache, `cache` in every identifier,
mechanically unrelated (it caches a static response body, nothing
authorization-related). It also has its own small commit history, including
one real fix commit and a handful of decoys, for trying `omen learn
--from-git` end to end:

```
python -m omen.cli scan demo_repo --all
python -m omen.cli learn --from-git HEAD --repo demo_repo --dry-run   # drop --dry-run to actually learn
```

### GPU notes

If you're running on a GPU-constrained card, model loading matters: Ollama
can silently split the model across CPU/GPU and tank throughput unless it's
told to keep the whole model resident on GPU. `omen/config.py` is the one
place model and runtime flags (`NUM_GPU`, `NUM_CTX`, `THINK_DEFAULT`) live —
adjust there if you're on different hardware.

## Commands

All commands go through `python -m omen.cli <command>`.

| Command | What it does |
|---|---|
| `omen seed <incidents.yaml>` | Loads a YAML file of incidents into the SQLite ledger. Used once to bootstrap a starter memory set. |
| `omen reindex` | Rebuilds the Chroma vector index from SQLite (embeds every incident's mechanism + surface forms). SQLite is always the source of truth; Chroma is a derived index you can throw away and rebuild any time. |
| `omen memory list` | Lists every incident currently in the ledger, with its ref and how it was learned. |
| `omen memory forget <ref>` | Deletes an incident from both SQLite and the vector index. This is the undo for a bad `omen learn` run. |
| `omen learn <postmortem.md>` | Reads a postmortem file and forms a new memory (or updates a near-duplicate one) from it. |
| `omen learn --from-git <range>` | Reads raw commit history (e.g. `HEAD~40..HEAD`) instead of a postmortem: a deterministic prefilter narrows the range to a handful of candidate commits, then each one is turned into a memory the same way. `--repo <path>` picks which repo to read history from (default: the current directory); supports `--max-commits`, `--max-learn`, and `--dry-run` (prefilter only, writes nothing). |
| `omen scan <path>` | Runs the full investigation pipeline against everything currently in memory and prints a verdict per flagged chunk. `--dry-run` stops after chunking (no embeddings, no model calls); `--retrieval-only` stops after showing ranked candidate incidents (no generation). `--since <rev>` diffs against a specific ref instead of the working tree; `--all` scans the whole tracked tree. |
| `omen calibrate` | Sweeps the retrieval similarity threshold over a labeled fixture set and prints where true positives stop surviving and hard negatives start getting rejected. A tuning tool for `SIMILARITY_THRESHOLD` in `config.py`, not part of the normal learn/scan workflow. |

### How they fit together

`learn` and `scan` are two ends of the same memory. `learn` is the only
thing that writes to the ledger — every `write_incident` call it makes
immediately re-embeds and rebuilds the vector index, so a freshly learned
incident is retrievable by the very next `scan` without a manual `reindex`.
`seed` and `reindex` exist to bootstrap and repair that same ledger by hand
(load a starter set, or rebuild the index after editing SQLite directly).
`memory list`/`forget` let you inspect and undo what's in there. `calibrate`
is a standalone tuning loop against a fixture file, independent of the real
ledger. In short: `seed`/`learn` put memories in, `reindex` keeps the index
honest, `scan` reads against it, and `memory` lets you see and undo what's
stored.

## Agent architecture

Omen runs one Gemma model, loaded once, with different prompts swapped in
per role — it isn't several separate models. Each role is a distinct agent
with a narrow job; the *order* they run in is fixed, ordinary Python, but
each individual role is free to reason and (for two of them) call tools
within its own turn.

### Learning a memory (`omen learn`)

1. **Sifter** *(git input only)* — a deterministic, no-LLM prefilter over a
   commit range. It flags commits by message pattern (`fix`, `revert`,
   `regression`, ...), revert detection, and touches to sensitive paths
   (auth, cache, payment, permission code), cutting a large range down to a
   handful of real candidates before any model call happens.
2. **Archivist** — reads either the postmortem file or a candidate commit's
   diff, and does the actual memory formation. Its central discipline is
   **abstraction**: it writes the failure down as a technology-agnostic
   *mechanism* ("a permission cache has no invalidation path tied to the
   revocation event"), not as a description of the specific library or
   framework involved — otherwise the memory could only ever match code
   that happens to use the same stack. For the git-history path specifically,
   it has to reconstruct the failure from the code's state *before* the fix
   commit, not describe the fix itself — a fix commit shows the remedy, not
   the bug. Before writing, it checks the existing ledger for a near-duplicate
   (`search_memory`) and updates that entry instead of creating a redundant
   one.

### Scanning a codebase (`omen scan`)

1. **Scout** — pure Python, no model. Resolves what's in scope (a diff, a
   ref range, or the whole tree) and chunks each file by AST into
   function/method/class-sized pieces the rest of the pipeline can reason
   about individually.
2. **Librarian** — embeds each chunk, queries the vector index for
   similar incident mechanisms, collapses hits down to one candidate per
   incident, and gates out anything below a calibrated similarity floor.
   Also pure Python — no generation, just retrieval.
3. **Triage** — the first model judgment. A fast, tool-free structured
   verdict (`MATCH` / `NO_MATCH`) on whether a chunk's own failure mechanism
   — reasoned independently, before comparing to the incident — actually
   matches a retrieved candidate, as opposed to just sharing surface
   vocabulary. Retrieval is deliberately generous, so Triage's default
   assumption is `NO_MATCH`; it only escalates chunks it can state a
   concrete mechanical reason for.
4. **Investigator** — runs only on Triage's `MATCH` chunks. This is the
   agentic step: it has tools (`read_code`, `grep_symbol`, `search_memory`,
   `get_incident`) to check things a single chunk can't show on its own —
   is there a mitigation elsewhere in the file, is this code actually
   reachable on the path the incident cares about. It doesn't rule
   MATCH/NO_MATCH itself; it produces an evidence-backed transcript for the
   next stage to judge.
5. **Adjudicator** — the final verdict: `confirmed`, `rejected`, or
   `unverified`. Deliberately blind to Triage's reasoning and verdict — it
   sees only the chunk, the candidate incidents, and the Investigator's
   transcript, so it can't just rubber-stamp the first stage's opinion. A
   `confirmed` verdict with no cited evidence lines is automatically
   downgraded to `unverified` in code, never left to the model's judgment.

Findings and run metadata are recorded to SQLite as the scan runs (visible
via the tables `runs`, `findings`, and `tool_calls`, the latter giving a
full audit trail of every tool call made during an investigation); a
rendered report beyond the terminal summary isn't built yet.

### Two execution backends

Every role runs through a `RoleRunner` interface with two interchangeable
implementations, selectable per-command with `--runner={adk,direct}`:
`ADKRoleRunner` (Google's Agent Development Kit) is the default, and
`DirectOllamaRunner` talks to Ollama directly as a fallback if ADK's
structured-output or tool-calling path misbehaves. Both are fully
implemented and tested equally, not one real path and one stub.

### Data model

SQLite is the source of truth for everything durable: incidents (with their
abstracted mechanism and concrete surface forms), a hash-cache of computed
embeddings, run records, findings, and the tool-call audit trail. Chroma
holds only a derived vector index for semantic retrieval — it's rebuilt
from SQLite by `omen reindex` and never written to directly, so it can be
deleted and regenerated at any time without losing anything.

## Testing

```
pytest                     # full suite; most tests need a reachable Ollama instance
pytest tests/test_scout.py # fast, no model required
```

Tests that require live inference are written that way deliberately —
prompt quality for a system like this can't be meaningfully verified with
mocks, so most of the suite exercises the real model rather than stubbing
it out.
