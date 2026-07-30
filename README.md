# Omen

An autonomous institutional-memory agent — a scoped-down, fully local rebuild
of [LORE](https://devpost.com/software/lore-living-organizational-record-engine)
for the *Agents on a Mission* track. Full design and rationale: [PLAN.md](PLAN.md).

Two missions, one local model (Gemma 4 via Ollama), one memory (SQLite + Chroma):

- `omen learn <postmortem.md>` / `omen learn --from-git <range>` — form memories
  from a written postmortem or directly from raw git history.
- `omen scan <path>` — investigate a codebase against those memories using tools,
  and explain why new code repeats an old failure.

## Status

Phase 0 complete (2026-07-30), on the RTX 5050 Laptop (8GB VRAM, Blackwell
sm_120, driver 595.95) — dev and demo are the same machine, so there is no
separate Mac tier. Python 3.11.9 venv (3.12 unavailable; 3.14 too new for
some wheels). See PLAN.md → "Project plan — 8 hours" for the build order and
"Verification" for acceptance checks per phase.

### Phase 0 results

- **Embedding dimension:** 768 (`embeddinggemma`, one `/api/embed` call).
- **Structured output (0b):** **10/10** schema-valid over 10 runs on a
  Triage-shaped prompt → build on ADK, per the plan's decision rule.
- **Tool calling (0b):** naive stub tools looped 0/10 (see below); with a
  file-level duplicate-call cache and realistic tool content, **4/4**
  correct on a smaller validation pass → tool calling itself is reliable,
  but `tools.py` (Phase 5) MUST implement the duplicate-call cache from the
  start, not as a later hardening pass — a model that gets an unsatisfying
  or repeated-looking tool result will re-call it until it hits the hard
  cap rather than concluding.
- **VRAM/perf gate (0c):** 12B Q4_K_M runs, but three things the plan didn't
  anticipate:
  1. **Ollama defaults to a partial CPU/GPU split** (~30/70) on this card at
     `num_ctx=8192`, giving only ~5 tok/s. Passing `num_gpu=999` explicitly
     forces full GPU residency (all 7.8GB on GPU) — do this always.
     `num_ctx=4096` fits comfortably at full GPU residency; 8192 does not.
  2. **Gemma 4's default "thinking" mode costs ~8x latency** (56s vs 7s on
     the same prompt, ~560 vs ~56 tokens) with no verdict-quality
     difference observed. `think=False` is required for latency-sensitive
     structured roles (Triage, Adjudicator) to hit the plan's ~8-10s
     target; the track's "reasoning" requirement is satisfied by the
     schema's `reasoning` field and the Investigator's tool trace, not by
     this hidden chain-of-thought.
  3. **The very first inference call after each Ollama restart can hard-crash**
     the CUDA backend (`llama-server process has terminated ... CUDA error:
     shared object initialization failed`), then succeed on immediate retry
     and stay stable. Reproduced twice. Mitigation: a throwaway warm-up
     call with one retry-on-connection-error, run at startup and before the
     demo — not merely the plan's "keep it warm for latency," but load-bearing
     for correctness.
- **Decision:** stay on `gemma4:12b-it-q4_K_M` (no E4B fallback needed).
  `config.py` defaults to `num_gpu=999`, `num_ctx=4096`, and `think=False`
  for Triage/Adjudicator (Investigator may want `think=True` for a richer
  live trace, at the accepted latency cost).

### Phase 1 results

`contracts.py`, `store.py` (SQLite schema + CRUD), `vectors.py` (Chroma,
`embedding_function=None`, telemetry off), and `incidents.yaml` (6 seed
incidents, 4 surface forms each) are in. A minimal `cli.py` exposes `seed`,
`reindex`, `memory list`, `memory forget` — the `scan`/`learn` subcommands
land in later phases.

- `omen memory list` shows all 6 seeded incidents.
- `omen reindex` produces 30 Chroma entries (6 incidents x 5 variants:
  1 mechanism + 4 surface forms each); running it twice yields the same
  count; deleting `omen_store/chroma` entirely and reindexing from SQLite
  alone reproduces the same 30 — confirms SQLite is genuinely the source
  of truth and Chroma is a derived index.
- `omen memory forget <ref>` removes an incident from both SQLite and
  Chroma (verified: 30 -> 25 on removing one 5-variant incident).
- **Retrieval sanity check** (ahead of Phase 4's formal calibration): a
  `functools.lru_cache`-shaped permission check, embedded with the
  retrieval-document prefix, returns OMEN-001 as all of its top 5 hits
  against the retrieval-query-prefixed incident variants — the
  EmbeddingGemma prefix convention (`task: search result | query: ...` for
  incident variants, `title: none | text: ...` for code) is correct.

### Phase 2 results

`scout.py`: git scope resolution (`diff` default, `--since <rev>`, `--all`,
non-git fallback with a warning) and `ast` chunking (top-level functions,
`Class.method` for methods, a whole-class chunk for method-less classes,
decorators included in the span, oversized functions split at 120 lines
with a 10-line overlap, module-level statements bundled into one
`<module>` chunk). Wired into `omen scan <path> --dry-run`.

14/14 tests pass (`tests/test_scout.py`), including the three the plan
requires (non-git dir, unparseable file, over-long function) plus extra
coverage on class methods, decorators, and content-hash determinism.

### Phase 3 results

`librarian.py`: hash-cached embedding (batches of 32, `omen/store.py`'s
`chunk_vectors` table), Chroma query at `n_results=6` on variant level,
collapse to best-variant-per-incident, cap at top 3, gate by similarity
(`SIMILARITY_THRESHOLD` in `config.py` — a placeholder until Phase 4's
fixture sweep). Wired into `omen scan <path> --retrieval-only`, the
zero-generation demoable artifact the plan wants at this point.

- **Manual end-to-end check**: a `functools.lru_cache`-decorated
  `has_access` (the planned demo true positive) retrieves OMEN-001 at
  0.507 similarity; the actual hard-negative function in a parallel
  `response_cache.py` (`get_static_page`, a real but unrelated cache)
  does **not** clear the threshold at all — only weaker incidental
  matches (a helper function, a bare `_cache = {}` declaration) do,
  which is expected at the retrieval-only gate and exactly what Triage's
  mechanism-first reasoning (Phase 6) exists to filter further.
- Cache verified: 0/4 hits on first run, 4/4 hits on an identical rerun.
- 23/23 tests pass (`tests/test_librarian.py` + `test_scout.py`):
  `collapse_to_incidents`/`gate` logic tested with no network dependency;
  the cache is tested with a monkeypatched embedder; one live end-to-end
  test (skips gracefully if Ollama is unreachable) pins the Phase 3
  acceptance criterion directly, against an isolated Chroma path so it
  can never touch the real seeded ledger.

## Setup

```
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# models already pulled: ollama list -> embeddinggemma, gemma4:12b-it-q4_K_M
python -m omen.cli seed incidents.yaml
python -m omen.cli reindex
python -m omen.cli memory list
```
