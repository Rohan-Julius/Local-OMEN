# OMEN — an autonomous institutional-memory agent

**Hackathon track:** *Agents on a Mission* — autonomous AI agents using Gemma that reason, use tools, retain memory, and automate complex multi-step tasks.

---

## Context

**The problem.** A codebase forgets. A team hits an incident, writes a postmortem, fixes it — and eighteen months later someone reintroduces the same failure mode written a different way. Linters can't catch it: the knowledge isn't a generic best practice, it's *this team's specific history*. Grep can't catch it either, because the second occurrence rarely reuses the first one's vocabulary.

**Prior art.** [LORE (Living Organizational Record Engine)](https://devpost.com/software/lore-living-organizational-record-engine) attacks this with an 8-agent router on the GitLab Duo Agent Platform, backed by Claude, running a five-layer merge-request review. Its writeup describes memory as *"structured ledger entries"* (`LORE-MEMORY-001`) synced into GitLab wiki pages — **it never mentions embeddings, a vector store, or a similarity algorithm.** Semantic matching is frontier-model reasoning over a catalog stuffed into the prompt; they mention condensing that catalog from 87 KiB to 31 KiB to fit a 64 KiB platform limit.

**Why this rebuild is architecturally different, not just smaller.** Prompt-stuffing is unavailable at 8GB of VRAM. Gemma 4 12B at Q4 with a capped context cannot hold a 31 KiB catalog against every file and stay coherent *and* fast. Retrieval is therefore not an optimization layered on top — it is the load-bearing element that makes the idea work at this budget. LORE is *stuff-and-reason*; this is *retrieve, investigate, then judge*.

**Why local is the product, not a compliance checkbox.** The corpus is proprietary source code joined to a written record of the team's own past failures. That second half is the sensitive part — an incident ledger is an attacker's roadmap and a competitor's dossier. The organizations most likely to want institutional memory are exactly the ones that cannot ship that pair to a third-party API. A cloud version of this tool is a *less useful* product, not merely a less private one.

**Intended outcome.** Two agent missions, both fully local: one that autonomously forms memories from postmortems, and one that autonomously investigates a codebase against those memories using tools, and explains why a new piece of code is the same failure as an old one.

---

## Track alignment

The four track capabilities, and the specific mechanism that delivers each. Written out because two of them were absent from the previous revision of this plan and were added deliberately.

| Capability | Mechanism | Where |
|---|---|---|
| **Reason** | Mechanism-level equivalence judgment: the agent states what the code can do wrong *before* ruling on whether that matches a past incident. Reasoning trace is streamed live. | Triage, Adjudicator |
| **Use tools** | Four local tools in the scan mission (`read_code`, `grep_symbol`, `search_memory`, `get_incident`), three in the learn mission. The agent decides which to call, in what order, and when it has enough. | Investigator, Archivist |
| **Retain memory** | SQLite ledger + Chroma index — and the agent **writes its own memories** via two input paths: a curated postmortem, or **raw git history with no human-written summary at all**. Includes dedup against what it already knows. Memory is read *and* written by the agent. | `omen learn`, `omen learn --from-git`, `write_incident` |
| **Automate multi-step tasks** | Two end-to-end missions. The tool loops are agent-terminated, not fixed-length: the agent decides when the investigation is complete. | Both missions |

**Where autonomy is and isn't used, and why.** The agent is genuinely autonomous *within* a stage — the Investigator decides which tools to call, in what order, how many times, and when to stop, and that loop terminates when the agent stops requesting tools. The *pipeline* order stays deterministic Python. This is a deliberate engineering position, not a shortcut: on this hardware every routing decision costs a ~10s inference, and the pipeline shape (scope → retrieve → triage → investigate → adjudicate → report) is fully known before the run starts. Autonomy is spent where the required steps are genuinely unknown in advance; determinism is kept where they aren't. An LLM planner choosing between known stages would buy variance and latency, not capability.

---

## Decisions locked

| Decision | Choice | Why |
|---|---|---|
| Dev vs demo host | Build on Mac, demo on RTX 5050 (8GB, Blackwell) | Drives the Ollama choice |
| LLM | Gemma 4 12B Q4_K_M, `num_ctx` 8192, KV cache `q8_0` | ~6.6GB; best reasoning in budget |
| Dev model | Gemma 4 **E4B** on the Mac | Inherits native function calling from the 31B; iterating a 12B on Metal would eat the day |
| Embedder | EmbeddingGemma 308M, q8 (~300MB) | Purpose-built on-device; strong on code retrieval |
| Runtime | Ollama for both | Identical HTTP API on Metal and CUDA |
| Agent framework | **Google ADK**, `LlmAgent` per role via `LiteLlm("ollama_chat/…")` | Google's own agent tooling; behind a strategy interface so it stays reversible |
| Tool calling | **Gemma 4 native function calling**, non-streaming | 86.4% on τ²-bench vs Gemma 3's 6.6%. Non-streaming avoids a known client-side `tool_calls` parsing bug |
| Contracts | `pydantic.BaseModel` | Required by ADK `output_schema`; one definition serves validation and schema generation |
| Vector store | ChromaDB `PersistentClient`, telemetry off, `embedding_function=None` | Real KNN + metadata filtering; see hardening |
| Chunk cache | SQLite content-hash table (not Chroma) | Exact-key lookup, never a similarity search |
| Scan scope | Local `git diff` default, `--all` opt-in | Platform-agnostic *and* fast |
| Chunking | Python `ast` only | Exact function/class boundaries, zero deps |
| Model residency | Sequential phases; embedder unloaded before generation | Satisfies "one model at a time" by pipeline design |

---

## Architecture

### Agent roster

Two missions share one model, one memory, and one tool layer.

**Mission 1 — `omen learn` · memory formation, two input paths**

```
omen learn <postmortem.md>        # curated path: a human wrote a document
omen learn --from-git <range>     # raw path: no human wrote anything
```

Both paths converge on the same Archivist role, the same dedup logic, the same `write_incident`, and the same reindex. Only the input adapter and the prompt variant differ.

| Role | Implementation | Tools | Job |
|---|---|---|---|
| **Sifter** (git path only) | pure Python, then optional ADK `LlmAgent` with `output_schema` | — | Reduce a commit range to the few commits that plausibly encode a remembered failure |
| **Archivist** | ADK `LlmAgent`, tools, no `output_schema` | postmortem: `read_file`, `search_memory`, `write_incident` · git: `read_commit`, `read_diff`, `search_memory`, `write_incident` | Abstract the failure mechanism away from its original technology; generate surface forms; **check memory for a near-duplicate first**; write new or update existing |

The duplicate check is why this role deserves agency rather than being a fixed script. A memory system that blindly appends is a memory system that degrades — and "search my own memory, decide whether this is new, then write" is a genuinely multi-step task whose steps depend on what it finds.

### The git path — the local analog of LORE's Decision Extractor

LORE's Decision Extractor fires automatically on merge and reads the raw diff plus the MR comment thread; **no human writes a summary first.** That's the half of LORE's self-learning story that a postmortem file doesn't reproduce — `omen learn <postmortem.md>` still needs someone to have written the postmortem. The git path closes that gap without reintroducing platform coupling: commit messages and diffs are local artifacts, available from `git` with no PR API, no webhook, no host.

Three design problems, and they're the substance of this feature.

**1. A fix commit shows the fix, not the failure.** This is the one that will silently produce garbage memories if unhandled. A commit that adds cache invalidation shows *invalidation being added* — the failure mechanism is defined by what was **absent before**. If the Archivist is simply pointed at a diff, it will faithfully record "this code invalidates its cache correctly," which is the exact inverse of a useful memory. The prompt must direct explicitly: *the failure is what the code did before this change; describe the pre-state, not the remedy.* `git show` supplies both sides of the diff, so the information is there — the model just has to be aimed at the right half. Revert commits are the richest source, because the message usually states what went wrong.

**2. Most commits are noise, and the Archivist is expensive.** A tool loop is ~30–45s. A 50-commit range run naively is over half an hour. So the Sifter reduces first, in two stages, cheapest first:

- **Deterministic prefilter (free, no inference).** Commit message patterns — `fix`, `hotfix`, `revert`, `regression`, `incident`, `postmortem`, `rollback`, `CVE` — plus structural signals: a commit that reverts another commit, or a diff touching auth / cache / payment / permission paths. Zero cost, and on a real repo it removes the large majority.
- **LLM triage pass (optional second stage).** A structured `output_schema` agent answering *"does this commit encode a failure worth remembering — yes/no, and one line why?"* at ~5s each. Build this **only if the deterministic prefilter proves too noisy in practice.** Start without it; the prefilter plus a hard cap may well be enough, and that's 20 minutes saved.

**3. Dedup gets harder, and ordering matters.** A postmortem run considers one document. A git range can yield several candidates, and multiple commits often address one underlying failure — the initial fix, a follow-up, the test that was added afterward. So: `search_memory` dedup runs per candidate as before, **and Chroma must be reindexed after each write rather than once at the end.** Otherwise the 40th commit cannot see the memory formed from the 12th, and you get three near-duplicate entries for one incident. Slightly slower, and correct.

**Caps and reversibility, because this writes to memory autonomously.** `--max-commits` (default 50) bounds the prefilter input; `--max-learn` (default 5) bounds Archivist invocations per run; `--dry-run` prints what it *would* learn and writes nothing. And a bad memory pollutes every future scan with no obvious undo, so `omen memory forget <ref>` ships alongside this — it is the safety valve that makes autonomous memory writes acceptable rather than reckless.

**Mission 2 — `omen scan <path>` · investigation**

| Role | Implementation | Tools | Job |
|---|---|---|---|
| **Scout** | pure Python | — | Resolve scope via local git; `ast`-chunk files with line spans |
| **Librarian** | pure Python + Ollama `/api/embed` | — | Embed chunks (hash-cached), query Chroma, collapse variants→incidents, threshold + cap |
| **Triage** | ADK `LlmAgent`, `output_schema`, no tools | — | Fast structured ruling on every gated chunk. Cheap, no tool round trips |
| **Investigator** | ADK `LlmAgent`, **tools**, no `output_schema` | `read_code`, `grep_symbol`, `search_memory`, `get_incident` | Only on Triage positives. Autonomously gather evidence until satisfied |
| **Adjudicator** | ADK `LlmAgent`, `output_schema`, no tools | — | Final verdict from the investigation transcript, **blind to Triage's reasoning** |
| **Chronicler** | ADK `LlmAgent` | — | Rewrite confirmed findings in persona |
| **Scribe** | pure Python | — | Persist to SQLite; render progress, terminal, and markdown reports |

### Why triage-then-investigate, rather than tools everywhere

Tools cost round trips. An Investigator loop is ~20–40s per chunk; running it on all 8 gated chunks would take four minutes. So the cheap structured Triage pass runs on everything and the expensive tool loop runs only on the 1–3 chunks where evidence could change the answer. **Tools are spent where they alter the outcome, not uniformly.**

This also produces something better than the "Skeptic" second-opinion pass in the previous revision. That design added a second *vote* on the same narrow context; this one adds *evidence*. The Adjudicator is still blind to Triage's reasoning — preserving the anti-echo property — but it now decides with the surrounding code in hand rather than re-guessing from the same chunk. It addresses the root cause of over-flagging (insufficient context) instead of averaging two under-informed opinions.

### ADK integration

```python
# omen/agents.py — the only module that imports google.adk
TRIAGE = LlmAgent(
    name="triage",
    model=LiteLlm(model=f"ollama_chat/{MODEL}"),   # ollama_chat, never ollama
    instruction=load_prompt("triage.txt"),
    output_schema=TriageOutput,     # pydantic → enforced JSON
    output_key="triage_result",
)

INVESTIGATOR = LlmAgent(
    name="investigator",
    model=LiteLlm(model=f"ollama_chat/{MODEL}", stream=False),   # see below
    instruction=load_prompt("investigator.txt"),
    tools=[read_code, grep_symbol, search_memory, get_incident],
    # no output_schema — deliberately, see below
)
```

Four details that matter:

**`ollama_chat/`, never `ollama/`.** ADK's docs warn the plain provider produces unexpected behavior.

**Tool-calling roles run non-streaming.** Gemma 4's tool calling works correctly at the Ollama API level on both the native and OpenAI-compatible endpoints, but there is a documented bug class where OpenAI-compatible *clients* fail to parse `tool_calls` in streaming mode. ADK reaches Ollama through LiteLLM's OpenAI-compatible path, which is exactly the affected shape. Disable streaming on any agent with tools.

**No agent has both `tools` and `output_schema`.** ADK documents that combination as supported only on specific models (Gemini 3.0 class), and its recommended remedy is to split output formatting into a separate agent. That is precisely the Investigator → Adjudicator split — so the framework constraint and the design agree rather than fight.

**Tool sets are scoped per mission *and per input path*, never pooled.** Three sets, each at most four tools: scan (`read_code`, `grep_symbol`, `search_memory`, `get_incident`), learn-from-postmortem (`read_file`, `search_memory`, `write_incident`), learn-from-git (`read_commit`, `read_diff`, `search_memory`, `write_incident`). Small-model tool-routing accuracy degrades as tool count grows, and reported Gemma 4 tool failures are mostly prompt/schema architecture rather than model weakness. Handing one agent all seven would be the easy mistake here. So: few tools per agent, `Literal` enums over free strings, every field marked required, and a one-line description on each parameter.

**Embeddings never touch ADK or LiteLLM.** The Librarian calls Ollama's `/api/embed` directly. Routing 34 embedding calls through an agent framework adds layers with nothing to gain.

### The reversibility hedge

ADK + LiteLLM + Ollama is three layers, and structured output and tool calling through all three are the two biggest unknowns here (risks 1 and 2). So role execution sits behind a strategy interface:

```python
class RoleRunner(Protocol):
    async def run_structured(self, role, prompt, schema: type[BaseModel]) -> BaseModel: ...
    async def run_tooled(self, role, prompt, tools: list[Callable]) -> Transcript: ...

class ADKRoleRunner:      ...   # google-adk + LiteLlm — the default
class DirectOllamaRunner: ...   # ollama client: format=schema, and native tools=[...]
```

`DirectOllamaRunner` is not hypothetical insurance. Ollama's `format` parameter accepts a JSON schema and **constrains decoding** to it — a stronger guarantee than instruct-and-validate — and its `/api/chat` returns `tool_calls` natively. If either ADK path proves unreliable with Gemma, `--runner=direct` switches implementations without touching a prompt or a contract. Build ADK first; wire the hatch from the start, because retrofitting it at hour six isn't something you'll have time for.

### Tools

All four are pure local Python — no network, and instant. The only cost is the LLM round trip that requests them.

```python
def read_code(file_path: str, start_line: int, end_line: int) -> str:
    """Read an exact line range from a file in the scanned repo."""

def grep_symbol(symbol: str) -> list[str]:
    """Find definitions and call sites of a symbol. Returns 'path:line: text'."""

def search_memory(query: str) -> list[str]:
    """Semantic search the incident ledger. Returns 'REF — title — mechanism'."""

def get_incident(incident_ref: str) -> str:
    """Full record for one incident: mechanism, what happened, the rule."""
```

Learn-mission tools:

```python
def read_file(file_path: str) -> str:
    """Read a postmortem or design document from disk."""

def read_commit(sha: str) -> str:
    """Commit message, author, date, and changed-file stat summary."""

def read_diff(sha: str, file_path: str) -> str:
    """Unified diff for one file in one commit, both sides, truncated to a line cap."""

def write_incident(...) -> str:
    """Create or update a ledger entry, then reindex. Returns the assigned ref."""
```

`read_code` and `grep_symbol` are the two that make tool use improve *accuracy* rather than merely satisfy a rubric. Without them the agent judges a chunk in isolation and cannot answer the question that decides most false positives: *is there an invalidation path elsewhere in this file, and is this function actually on an auth path?* `search_memory` is the agent using its own memory as a tool — the most legible agentic moment in the demo. `get_incident` exists so incidents discovered via `search_memory` can be pulled in full.

**Hard caps, because an agent loop is an unbounded loop.** Max 6 tool calls per chunk, max 2 calls to the same tool with the same arguments (repeat requests are answered from a cache with a note), and a wall-clock ceiling per investigation. All three enforced in the orchestrator, not requested in the prompt.

**Path confinement.** `read_code` and `grep_symbol` resolve paths against the scanned repo root and reject anything outside it. The agent is reading attacker-adjacent content (source code) and choosing its own paths; a traversal escape would let a crafted repo read arbitrary files. Cheap to prevent, so prevent it.

### Model residency

The one-model constraint dictates the topology rather than being worked around.

```
      ┌──────────┐
~0.3G │EmbedGemma│ ── keep_alive:0 ──► unloaded
      └──────────┘
                    ┌─────────────────────────────────────────────┐
~7.5G               │ Gemma 4 12B Q4_K_M  (loaded once, kept warm) │
                    └─────────────────────────────────────────────┘
      Scout Librarian │ Triage → Investigator → Adjudicator → Chronicler │ Scribe
      (cpu) (embed)   │      all four share these resident weights       │ (cpu)
```

**Batch-then-switch, never interleave.** All embedding completes before any generation begins, so the two models never contend. And four LLM roles cost one model load, not four — prompt swaps against resident weights are free, which is exactly why four roles are affordable where four *models* would not be.

### Degradation ladder

Every rung produces a useful report. Ordered by what to shed first.

| Mode | What runs | Time |
|---|---|---|
| default | full agentic pipeline with tools | ~90–120s |
| `--fast` | Triage only, no tool loop | ~40s |
| `--no-persona` | drops the Chronicler | −10s |
| `--retrieval-only` | Librarian output as a ranked candidate list, zero generation | ~2s |

`--retrieval-only` is the on-stage safety net: if generation misbehaves live, it still demonstrates semantic retrieval ranking the right incident.

### Versus LORE

| | LORE | This build |
|---|---|---|
| Framework | GitLab Duo Agent Platform | Google ADK → LiteLLM → Ollama, fully local |
| Control flow | LLM router across 8 agents | Deterministic pipeline; agentic tool loops within stages |
| Memory access | full catalog in prompt (31 KiB) | HNSW retrieval, top-3, plus `search_memory` as a tool |
| Memory formation | post-merge decision extraction | `omen learn` with autonomous dedup |
| Cost per routing decision | one inference | zero |
| Testability of control flow | requires model calls | plain unit tests |

---

## Honest assessment

**Can Gemma 4 at Q4 judge failure-mode equivalence reliably? Partially — and the framing decides the answer.**

It will do well at the constrained form: given a specific chunk and a specific mechanism-level incident, ruling "same mechanism / different mechanism" with both in context is within a 12B model.

It will fail at **over-flagging.** Asked "is this related?", a small model finds everything vaguely related and agrees with the premise it was handed. Unmitigated, that yields twelve findings per scan of which one is real — worse than no tool, and it reads as broken on stage. Every choice below that looks like paranoia targets this specific failure:

1. **Two independent gates.** A finding needs embedding similarity above threshold *and* an affirmative model verdict. Neither alone can flag.
2. **Mechanism before verdict.** The prompt forces a statement of what the code can mechanically do wrong *before* the matching question. Reversing the order is the difference between reasoning and rationalizing.
3. **Evidence, not just a second opinion.** The Investigator reads the surrounding code, so the Adjudicator decides with context that the Triage pass lacked.
4. **`NO_MATCH` is the declared default** and must be argued out of. The prompt states most code matches no incident and that a wrong flag costs more than a miss.
5. **Required line-level evidence**, auto-downgraded in code when empty — never left to the model's discretion.
6. **Hard negatives in the prompt:** two worked examples of superficially-similar-but-mechanically-different code.
7. **Persona quarantined** in the Chronicler, so voice cannot leak into judgment.

**Calibration is a build task, not a hope.** 12 labeled fixture chunks (4 true matches, 4 hard negatives, 4 unrelated), and the threshold is chosen from a sweep, not guessed.

**On tool calling specifically:** Gemma 4's 86.4% τ²-bench tool accuracy is good but not perfect, and it is a benchmark, not a promise about this prompt. Phase 0 measures it on *our* actual tool schemas before anything is built on it. The mitigation if it disappoints is not prompt heroics — it's `--fast` (skip the tool loop) plus `--runner=direct`, both of which are pre-wired.

---

## Data model

### SQLite — source of truth

```sql
CREATE TABLE incidents (
  id                INTEGER PRIMARY KEY,
  ref               TEXT UNIQUE NOT NULL,   -- 'OMEN-001', appears in reports
  kind              TEXT NOT NULL DEFAULT 'incident',
  title             TEXT NOT NULL,
  failure_mechanism TEXT NOT NULL,   -- abstracted off the original tech. Embedded.
  what_happened     TEXT NOT NULL,
  the_rule          TEXT NOT NULL,
  severity          TEXT,
  occurred_on       TEXT,
  source            TEXT,            -- provenance: 'postmortem:path' | 'git:abc1234f'
  learned_by        TEXT,            -- 'seed' | 'archivist:postmortem' | 'archivist:git'
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE surface_forms (         -- concrete manifestations; each embedded separately
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  form TEXT NOT NULL
);

CREATE TABLE chunk_vectors (         -- code-side cache. Exact-key only; not in Chroma
  content_hash TEXT NOT NULL, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vec BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (content_hash, model)
);

CREATE TABLE runs (                  -- you cannot optimize latency you don't measure
  run_id TEXT PRIMARY KEY, mission TEXT NOT NULL,   -- 'scan' | 'learn'
  repo_path TEXT, scope TEXT, llm_model TEXT, embed_model TEXT, runner TEXT,
  started_at TEXT, finished_at TEXT,
  n_files INTEGER, n_chunks INTEGER, n_cache_hits INTEGER,
  n_gated INTEGER, n_triaged INTEGER, n_investigated INTEGER,
  n_tool_calls INTEGER, n_confirmed INTEGER,
  ms_embed INTEGER, ms_triage INTEGER, ms_investigate INTEGER, ms_total INTEGER
);

CREATE TABLE findings (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  incident_ref TEXT NOT NULL, file_path TEXT NOT NULL,
  start_line INTEGER, end_line INTEGER, symbol TEXT,
  similarity REAL NOT NULL,
  verdict TEXT NOT NULL,             -- confirmed | unverified | rejected
  code_mechanism TEXT, reasoning TEXT, evidence TEXT, confidence TEXT,
  tool_calls INTEGER,                -- how much evidence this verdict rests on
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE tool_calls (            -- the agent's audit trail; also the demo artifact
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  chunk_symbol TEXT, seq INTEGER,
  tool_name TEXT NOT NULL, args_json TEXT, result_summary TEXT, ms INTEGER
);
```

`learned_by` is small but carries the track's weight: it distinguishes a memory the agent formed from one a human seeded, **and which input path formed it** — `archivist:git` means no human wrote a summary at all. `omen memory list` prints it, and `source` carries the SHA so any learned memory is traceable back to the commit it came from.

The `tool_calls` table is the audit trail. It's also how you answer "did the agent actually use tools, or did you just say it did?" — the answer is a query.

### Chroma collection: `incidents`

One record per **embedded variant** — the mechanism sentence plus each surface form — so an incident's score is the max over its variants.

| Field | Value |
|---|---|
| `id` | `"OMEN-003::mechanism"`, `"OMEN-003::surface:2"` — deterministic, so re-seeding upserts |
| `embedding` | EmbeddingGemma vector, L2-normalized, always supplied explicitly |
| `document` | the variant text — lets you debug retrieval by eye |
| `metadata` | `{incident_ref, incident_id, variant, kind, severity}` |

**SQLite is the source of truth; Chroma is a derived index.** `omen reindex` rebuilds it. Never write Chroma directly — a vector index that has silently drifted from the ledger is very hard to diagnose under time pressure, and this makes it a one-command fix.

### Chroma's two network defaults — both must be off

1. **Telemetry ships on.** Set `ANONYMIZED_TELEMETRY=FALSE` *and* pass `Settings(anonymized_telemetry=False)` — belt and braces, because an env var is easy to lose moving from Mac to the 5050.
2. **The default embedding function downloads a model.** A collection created without one instantiates `ONNXMiniLM_L6_V2` and fetches it from S3. Always `embedding_function=None`.

```python
client = chromadb.PersistentClient(path="./omen_store/chroma",
                                   settings=Settings(anonymized_telemetry=False))
col = client.get_or_create_collection("incidents", embedding_function=None,
                                      metadata={"hnsw:space": "cosine"})
```

`hnsw:space` is not bookkeeping — Chroma defaults to L2, and leaving it there makes every calibrated threshold wrong in a way that reads like a bad prompt. **Chroma returns distances, not similarities**; convert once at the `vectors.py` boundary so no threshold logic downstream sees a raw distance.

### Why `surface_forms` is the single biggest retrieval lever

Embedding one abstract mechanism sentence and hoping concrete code lands near it is weak — the abstraction gap is exactly where cosine similarity underperforms. Each incident instead carries 3–5 concrete surface forms **deliberately spanning technologies the incident never involved**: the Redis-token incident gets forms for `dict`, `functools.lru_cache`, `cachetools.TTLCache`, and a local file cache. This is what converts "semantic matching" from a claim into a mechanism, and it's the Archivist's most important output when it forms a new memory.

---

## Latency

Estimates to validate in phase 0, not facts.

| Stage | Cost | Scaling risk | Mitigation |
|---|---|---|---|
| Model load (cold) | 5–15s | one-time | keep warm; pre-warm before demo |
| Embed | ~1–3s / 300 chunks | linear in changed chunks | hash cache → ~0 on re-scan |
| Chroma KNN | <50ms, one batched query | sub-linear; fine to 10k+ incidents | none needed |
| Triage | ~8s × gated chunks | **linear — capped at 8** | `--max-gated`; short `num_predict` |
| **Investigator** | **~20–40s × positives** | **the wall: unbounded agent loop** | **max 6 tool calls, max 2 positives, wall-clock ceiling** |
| Adjudicator | ~10s × positives | linear in positives (few) | only runs on investigated chunks |
| Chronicler | ~10s, once | constant | skipped when no findings |

**Revised targets, stated honestly: ~90–120s for a full agentic scan, ~40s with `--fast`.** The previous revision targeted 45s; adding a tool loop makes that unachievable, and pretending otherwise would just mean discovering it on stage. 90s is still inside what a developer will wait for, and `--fast` exists for the everyday path where tools rarely change the answer.

Scaling failures, stated plainly:
- **Agent loop length** — the new dominant risk. An agent that keeps requesting tools runs forever. Hard caps are enforced in the orchestrator, never merely requested in the prompt.
- **File count** — why `git diff` is the default. `--all` prints an honest time estimate before starting.
- **Output length** — unbounded `num_predict` is the most common way small-model pipelines get slow. Structured schemas plus token caps keep generations short.
- **Incident count** — effectively free. HNSW is sub-linear and only the top 3 reach the model, so the ledger grows to thousands without slowing a scan. Worth saying on stage: the memory scales, the scan doesn't.

---

## Cut list

**Cut from LORE:** *promise verification* (needs an issue tracker — the platform coupling being removed); *Security Sentinel* (generic best-practice checks that contradict LORE's own thesis and duplicate linters); *code intelligence / dependency drift*, *pre-mortem generation*, *developer response evaluation*, *conversational ask*, *onboarding briefings*, *HTML dashboard + Mermaid graph*, *8-agent router*.

**Cut from this build:** web UI, PR/MR event wiring, multi-language parsing, fine-tuning, `git_log` as a tool.

**Stretch, only after the demo is rehearsed:** reviewer-rule enforcement (`kind='rule'` reuses the whole pipeline with one different prompt); memory evolution when a decision is overridden.

---

## Module layout

```
omen/
├── omen/
│   ├── cli.py          # argparse + asyncio.run; the deterministic orchestrator
│   ├── contracts.py    # pydantic models. No logic, no third-party imports
│   ├── config.py       # ONLY place models are named. 12B→E4B is one line
│   ├── agents.py       # ONLY place google.adk is imported. The 5 LlmAgents
│   ├── runners.py      # RoleRunner protocol + ADKRoleRunner + DirectOllamaRunner
│   ├── tools.py        # the 7 tool functions + path confinement + call caps
│   ├── store.py        # SQLite: ledger, cache, runs, findings, tool_calls
│   ├── vectors.py      # ONLY place chromadb is imported. Owns distance→similarity
│   ├── scout.py        # git scope resolution + ast chunking
│   ├── librarian.py    # embed (cached, direct /api/embed) + query + collapse + gate
│   ├── archivist.py    # the learn mission
│   └── report.py       # progress lines, terminal + markdown reports
├── prompts/            # triage, investigator, adjudicator, chronicler, archivist
├── incidents.yaml      # the seeded ledger
├── postmortems/        # raw markdown for `omen learn` to consume
├── fixtures/           # labeled chunks + expected verdicts
├── demo_repo/          # the codebase scanned on stage
└── tests/
```

**Four chokepoints**, each isolating one reversible decision:

| Module | Isolates | The one-line escape |
|---|---|---|
| `config.py` | which Gemma | 12B → E4B on OOM |
| `runners.py` | ADK vs. direct | `--runner=direct` if ADK's schema or tool path is flaky |
| `agents.py` | all of `google.adk` | nothing else imports the framework |
| `vectors.py` | all of `chromadb` | no unhardened client, no raw distances downstream |

Three of this plan's biggest risks — VRAM fit, ADK reliability, Chroma's network defaults — are each confined to one file, so any can be backed out in minutes instead of becoming an architectural problem at hour six.

**Prompts live in files, not string literals.** You'll edit them dozens of times during calibration; keeping them outside the Python means they diff cleanly and a bad edit never breaks an import.

---

## Project plan — 8 hours

Every phase ends in something runnable and has an explicit acceptance check. **Phases 0 and 4 are go/no-go gates.**

### Phase 0 · Environment and two spikes (0:00–1:00)

**0a — this Mac.** Ollama 0.32.4 is installed; only `qwen3:4b` is pulled, and Python 3.12.2 is available (system 3.9 — don't use it).
- `python3.12 -m venv .venv`; install **pinned**: `google-adk`, `litellm`, `chromadb`, `pydantic`, `ollama`, `pyyaml`.
- `ollama pull embeddinggemma`, `ollama pull gemma4:e4b`.
- One `/api/embed` call; record the embedding dimension.

**0b — the two ADK spikes (~30 min, do not skip).** Throwaway scripts, no project code.
- *Structured output:* an `LlmAgent` with `output_schema` on a realistic judging prompt, **10 runs**, count schema-valid returns.
- *Tool calling:* an `LlmAgent` with two of the real tool signatures, non-streaming, **10 runs**, count correct tool invocations.

> **Decision rules.** Structured: 10/10 → build on ADK; 8–9/10 → build on ADK with the retry path mandatory; <8/10 → `--runner=direct` becomes the default. Tools: ≥8/10 → proceed; <8/10 → `--fast` becomes the default mode and the Investigator becomes a demo-only path. **Record both numbers in the README** — they're the honest answer to "does this actually work," and they took twenty minutes to get.

Also confirm here: no network egress (set `GOOGLE_GENAI_USE_VERTEXAI=FALSE`, leave `GOOGLE_API_KEY` unset), and that ADK is async, so `cli.py` is `asyncio.run(main())`.

**0c — the 5050, before the demo (VRAM gate).** `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, pull 12B Q4_K_M, one ~3K-token prompt at `num_ctx 8192`, watch `nvidia-smi`, record tok/s.
> If it OOMs or runs below ~8 tok/s: set E4B in `config.py` and move on. Don't fight a tight VRAM fit on demo day.

*Acceptance:* embedding dimension recorded; both spike numbers recorded and the runner selected; installs verified with the network off.

### Phase 1 · Contracts, store, vectors, seed (1:00–2:00)

- `contracts.py` first — no dependencies, everything imports it.
- `store.py`: full schema, `omen seed incidents.yaml`, `omen memory list` (printing `learned_by`).
- `vectors.py`: hardened client, upsert, batched query, distance→similarity. **Then run the network-isolation test immediately** — catching a stray default embedding function now costs minutes; at hour seven it costs the demo.
- `incidents.yaml`: 6 incidents, 3–5 surface forms each. **Highest-leverage work of the day** — thin surface forms cap everything downstream. (6 not 8: one more incident arrives via `omen learn` in the demo.)

*Acceptance:* `omen memory list` shows 6; Chroma count equals total variants; reindex twice yields the same count; seed + reindex both work offline.

### Phase 2 · Scout (2:00–2:25)

Pure Python, no models, fully unit-testable.
- Scope: `git diff --name-only` default, `--since <rev>` for a range, `--all` full tree, automatic full-tree fallback with a notice when the path isn't a git repo.
- `ast` chunking: `FunctionDef`/`AsyncFunctionDef`/`ClassDef`, exact line spans, `Class.method` symbols, ~120-line cap with 10-line overlap, one synthetic chunk for module-level code, syntax errors warned and skipped.

*Acceptance:* `omen scan --dry-run` prints a correct chunk table. Tests cover non-git, unparseable file, over-long function.

### Phase 3 · Librarian (2:25–3:15)

- Embed with the correct EmbeddingGemma prefixes — retrieval-document for chunks, retrieval-query for incident variants. Omitting or swapping them silently costs recall.
- Content-hash cache; batches of 32.
- Chroma query at `n_results=6` **variant** level, collapse to best-variant-per-incident, take top 3. Querying k=3 directly is a bug: three variants of one incident can fill all three slots and starve the candidate list to one.
- `--retrieval-only` ships here — **you have a demoable artifact at 3:15.**

*Acceptance:* the known-positive chunk retrieves its incident in the top 3. Print raw variant hits: if only surface forms fire and the mechanism sentence never ranks, that's real signal about `incidents.yaml`. Second run: 100% cache hits.

### Phase 4 · Fixtures and calibration — GO/NO-GO (3:15–3:45)

12 labeled chunks: 4 true matches, 4 hard negatives, 4 unrelated. Sweep the similarity floor; pick the knee. Record the number.

*Acceptance:* **all 4 true positives survive retrieval and at most 2 of the 4 hard negatives do.**
> **If this fails, the problem is `incidents.yaml`, not the threshold.** Add surface forms. Do not proceed hoping the LLM compensates — it cannot see what retrieval discarded.

### Phase 5 · Tools (3:45–4:15)

Pure Python, no LLM — so this is fast and fully testable before any agent depends on it.
- The four scan tools + three learn tools; `Literal` enums where possible, every parameter described and required.
- Path confinement against the repo root; reject traversal.
- Call caps, duplicate-call cache, wall-clock ceiling — **in the orchestrator, not the prompt.**
- Every invocation logged to `tool_calls`.

*Acceptance:* unit tests per tool including a traversal attempt and a duplicate-call hit. Tools callable from a REPL with no model running.

### Phase 6 · Triage + Investigator (4:15–5:30)

- `agents.py`: Triage (`output_schema`, no tools) and Investigator (tools, non-streaming, no `output_schema`).
- `runners.py`: both implementations. Wire `DirectOllamaRunner` now even if the spike said ADK is fine — it's ~20 lines and it's the difference between a five-minute recovery and a dead end.
- Prompts: mechanism before verdict; `NO_MATCH` declared as default; two worked hard negatives; required evidence lines.
- Investigator loop driven by the orchestrator: run, execute requested tools, feed results back, repeat until the agent stops asking or a cap trips. **Stream the reasoning and tool calls to the terminal live.**

*Acceptance:* fixture set through Triage; record precision/recall. Watch one investigation end-to-end and confirm the tool choices are sensible, not random. Temperature 0 gives identical Triage verdicts across runs.

### Phase 7 · Adjudicator (5:30–6:10)

- Own prompt, own `output_schema`. Receives the chunk, the incident, and the **investigation transcript** — with Triage's reasoning and verdict stripped out.
- Empty `evidence_lines` on a positive → auto-downgraded in code.
- Agreement → `confirmed`; disagreement → `unverified`, reported separately, never silently dropped.

*Acceptance:* fixture precision improves or holds vs. phase 6. Assert the assembled prompt contains neither Triage's `reasoning` nor its verdict — a string assertion, because a leak looks like success from the outside.

### Phase 8a · `omen learn <postmortem.md>` (6:10–6:50)

- `archivist.py` + the Archivist agent with `read_file`, `search_memory`, `write_incident`.
- Prompt emphasizes the two things that matter: **abstract the mechanism off its original technology**, and **generate surface forms spanning technologies the incident never used.**
- Dedup: search memory first; if a near-duplicate exists, update rather than append.
- Write with `learned_by='archivist:postmortem'`, reindex, then `omen memory forget <ref>` as the undo.
- Author one postmortem markdown in `postmortems/` for the demo.

*Acceptance:* produces an incident whose surface forms include a technology absent from the source document — that's the abstraction working. Running it twice does not create a duplicate.

### Phase 8b · `omen learn --from-git <range>` (6:50–7:35)

Only ~45 min of genuinely new work, because the Archivist role, dedup, write, reindex, and undo all already exist from 8a. **Do not start this before 8a's acceptance passes** — a half-built second input path is worse than one working path.

- `sifter.py`: the deterministic prefilter only. Message patterns + revert detection + sensitive-path signals. **Skip the LLM triage stage** unless the prefilter proves too noisy; that's a 20-minute saving taken by default.
- Two new tools: `read_commit`, `read_diff` (both with a line cap — a large diff will blow the 8K context).
- `prompts/archivist_git.txt`: a variant whose central instruction is **describe the pre-change failure, not the fix.** This is the prompt that decides whether the feature works; budget most of the phase here.
- Reindex after *each* write, so later commits in the range can dedup against earlier ones.
- Caps: `--max-commits` 50, `--max-learn` 5, `--dry-run`.
- **Construct git history in `demo_repo/`** — a real fix commit with a plausible message, plus decoy commits the prefilter should reject. This is a hidden cost: you need actual commits, not just files. ~15 min of scripted `git commit`.

*Acceptance:* on the demo repo's history, the Sifter reduces N commits to ≤3 candidates; the Archivist forms a memory whose `failure_mechanism` describes the **absent invalidation**, not the added invalidation; `source` records the SHA; a second run produces no duplicate. `--dry-run` writes nothing.

### Phase 9 · Report and demo repo (7:35–7:50)

Compressed to fund phase 8b. The Chronicler persona pass moves to stretch — see the revised cut list.

- Report: per-role progress with live timings, terminal summary, persistence. **Terminal only; markdown report deferred.** Screen and DB timings come from the same measurements, so they cannot disagree.
- `demo_repo/`: ~5 files of plausible Python service code containing exactly **one true positive** (same mechanism as a learned incident, different library, no shared vocabulary — `functools.lru_cache` on a permission lookup) and **one hard negative** (a genuine cache, "cache" all over the identifiers, mechanically unrelated — caching a static response body), plus ~3 ordinary files.

*Acceptance:* full scan renders progress, tool calls, and a finding naming the incident ref, file, line span, and mechanism. `grep -ri redis demo_repo/` returns nothing and the scan still flags the right chunk.

### Phase 10 · Harden and rehearse (7:50–8:00)

- Network-isolation test, full pipeline, on the demo machine. **Three libraries in this stack default to talking to the internet** — this is the only thing that proves they don't.
- Degradation: `--fast`, `--no-persona`, `--retrieval-only`, `--runner=direct` all produce valid output.
- Graceful failure: non-git dir, syntax-error file, empty ledger, a tool-call cap trip mid-investigation.
- **Rehearse three times, model pre-warmed.** Keep `--retrieval-only` in shell history.

### If you fall behind, cut in this order

The schedule is now genuinely full — 8b takes it to 7:35 with 25 minutes of slack. Cuts, in order:

1. **The Chronicler persona pass** — already moved to stretch. Findings render fine without a voice.
2. **Markdown report** — already deferred; terminal only.
3. **Phase 8b entirely.** If you reach 6:50 without 8a passing acceptance, ship postmortem-only and say so plainly in the writeup: you implemented memory *formation*, not LORE's fully-automatic *acquisition*. That's an honest, defensible scope statement — a half-working git path is not.
4. **`--fast` as the default** — drops the Investigator from the scan path. Tools stay in the repo and stay honest to describe.
5. **`--runner=direct`** — drops ADK from execution, keeps `agents.py` as the described architecture.
6. `demo_repo/` from 5 files to 3.

**Never cut:** phase 0's two spikes, phase 4's calibration, the hard negative in `demo_repo/`, the tool-call caps, `omen memory forget`, or the network-isolation test. Those are what make the claims true rather than asserted — and `forget` in particular, because autonomous memory writes without an undo is a bad trade at any schedule.

---

## The 3-minute demo — a closed loop

The loop is the point. A tool that only *reads* a memory is a linter with extra steps; one that *forms* a memory and then catches a repeat is an agent with institutional memory. And the negative control is not optional — a demo that only shows a catch proves nothing, because `grep cache` also "catches" it.

| Time | Beat |
|---|---|
| 0:00–0:15 | `omen memory list` — 6 incidents, all `learned_by=seed`. This is what a human put in. |
| 0:15–1:00 | **`omen learn --from-git HEAD~40..HEAD`.** Nobody wrote a postmortem. The Sifter reduces 40 commits to 2 candidates; watch the Archivist call `read_commit`, then `read_diff`, then `search_memory` to check whether it already knows this, then `write_incident`. Show the formed memory: from a commit that *added cache invalidation*, it recorded the failure as **the absence of invalidation** — the inverse of what the diff literally shows — with no Redis in the mechanism statement and surface forms naming libraries the commit never touched. `omen memory list` now shows 7, newest `learned_by=archivist:git`, `source=git:abc1234f`. **No human wrote a word of that memory.** |
| 1:00–1:20 | The offending file in `demo_repo/`: a `functools.lru_cache` on a permission lookup. No Redis, no TTL, no shared vocabulary. `grep -ri redis demo_repo/` → nothing. |
| 1:20–2:15 | `omen scan ./demo_repo`. Live: Triage flags it, then the Investigator autonomously calls `read_code` to check for an invalidation path and `grep_symbol` to confirm the function sits on an auth path. Adjudicator confirms with line-level evidence. It names the incident it taught itself **sixty seconds ago from a commit** and explains the mechanism: an authorization decision cached with no invalidation path, so revocation doesn't take effect. |
| 2:15–2:40 | **The negative control.** `cache/response_cache.py` — a real cache, "cache" everywhere, not flagged. Show *why* from the transcript: the Investigator read it, found no authorization decision in the cached path, and rejected it. **That is semantic over keyword, demonstrated rather than claimed.** |
| 2:40–3:00 | Nothing left the machine — re-run with the network off. Proprietary source joined to a record of the team's own failures is exactly the pair that cannot go to an API. Close on scaling: the ledger grows to thousands without slowing a scan, because retrieval is sub-linear and only the top 3 reach the model. |

**On-stage fallback:** `omen scan --retrieval-only` runs in ~2s and still shows semantic retrieval ranking the right incident. Have it in your history before you start.

---

## Verification

- **Fixture set (primary).** `pytest` over 12 labeled chunks; ≥3/4 true positives caught, ≤1/4 hard negatives flagged. Gates every prompt change — a prompt edit that lifts recall while wrecking precision must fail here, not on stage.
- **Schema and tool compliance rates.** Phase 0's two spikes promoted to permanent tests, recorded per runner. Regressions here are silent: they look like the model getting worse, not the framework changing.
- **Runner equivalence.** The fixture set through both runners should agree. Divergence means one path is mangling the prompt or schema — trust the constrained-decoding path.
- **Tool-loop bounds.** Assert the caps trip: a chunk that keeps requesting tools stops at 6 calls, duplicate calls hit the cache, and the wall-clock ceiling fires. An unbounded agent loop is the one bug that can hang a live demo.
- **Path confinement.** `read_code("../../.ssh/id_rsa", 1, 5)` must be rejected.
- **Adjudicator independence.** Assert the assembled prompt contains neither Triage's `reasoning` nor its verdict.
- **No agent has both tools and `output_schema`.** A constructor-level assertion, guarding the documented ADK incompatibility so it surfaces as a clear error rather than confusing schema failures.
- **Memory formation.** `omen learn` twice on the same postmortem creates one incident, not two. Its surface forms mention at least one technology absent from the source.
- **Git-path inversion (the one that matters most for 8b).** Given a commit that *adds* an invalidation call, assert the resulting `failure_mechanism` describes the **missing** invalidation, not the added one. A memory that records the fix instead of the failure is worse than no memory, and it will read as plausible unless specifically checked.
- **Sifter precision.** On the demo repo's constructed history, the deterministic prefilter must reduce N commits to ≤3 candidates and must reject the decoy commits (version bumps, typo fixes, test additions).
- **Cross-commit dedup.** Two commits in one range addressing the same underlying failure must yield one incident. This is the test that catches reindex-at-the-end instead of reindex-per-write.
- **Undo.** `omen memory forget <ref>` removes the entry from SQLite *and* its variants from Chroma; a subsequent scan no longer matches it.
- **`--dry-run` writes nothing.** Assert row counts in `incidents` and the Chroma collection are unchanged after a dry run.
- **Network isolation.** Full pipeline, both missions, interface disabled. The only real proof of end-to-end locality with `google-adk`, `litellm`, and `chromadb` in the stack.
- **Chroma integrity.** Delete the Chroma directory, `omen reindex` from SQLite, confirm findings unchanged — proves SQLite is genuinely the source of truth.
- **Determinism.** Temperature 0; identical Triage verdicts across runs.
- **Latency.** `ms_total` under 120s full, under 45s `--fast`, under 20s warm on re-scan.
- **Graceful degradation.** Non-git dir, syntax-error file, empty ledger, cap trip mid-investigation — clear messages, no tracebacks.

---

## Open risks

1. **ADK structured output through LiteLLM to Ollama.** Three layers between `output_schema` and the model, and ADK's own docs example *also* puts "respond ONLY with JSON" in the instruction — suggesting instruct-and-validate rather than constrained decoding. Ollama's native `format=<schema>` genuinely constrains. Phase 0b turns this into a number; `runners.py` lets you act on it in one line.
2. **Gemma 4 tool calling at 86.4%, not 100% — and that's a benchmark, not a promise about our prompts.** Measured in phase 0b on the real tool schemas. Mitigations are pre-wired (`--fast`, `--runner=direct`) rather than improvised. Also: tool-calling roles **must** run non-streaming, because there is a documented bug class where OpenAI-compatible clients mis-parse streaming `tool_calls`, and ADK→LiteLLM is exactly that shape.
3. **The agent loop is the new latency wall and the new hang risk.** Caps on call count, duplicate calls, and wall-clock are enforced in the orchestrator. A prompt that politely asks the agent to stop is not a bound.
4. **Over-flagging remains the likeliest way the demo fails.** Seven mitigations are built in; the fixture set is the tripwire. If precision can't clear ~75% on fixtures, raise the similarity floor and accept lower recall — one real finding beats twelve maybes.
5. **VRAM fit on 12B Q4 is genuinely tight** at 8GB with Windows reserving some. Phase 0c settles it; the E4B fallback is pre-planned, one line.
6. **Three libraries default to network activity.** Chroma telemetry, Chroma's auto-downloading default embedding function, and ADK/LiteLLM credential probing. All handled in code; only the isolation test proves it.
7. **Seed and learned memory quality is the ceiling on retrieval.** Thin surface forms can't be recovered by prompt work. This is also the Archivist's hardest job — judge its output by whether the surface forms name technologies the source postmortem didn't.
8. **Scope has grown, and the schedule is now genuinely full.** Two missions, three input paths, five LLM roles, seven tools, 8 hours. Phase 8b runs to 7:35 with only 25 minutes of slack, funded by moving the Chronicler to stretch and deferring the markdown report. The cut list is ordered so phase 8b can be abandoned cleanly at 6:50 without leaving the demo broken.
9. **The git path can confidently record the inverse of the truth.** A fix commit shows the remedy; the memory needs the failure. If the prompt doesn't aim the model at the pre-change state, it will produce fluent, plausible, exactly-wrong memories — and unlike a crash, this failure looks like success. It is caught only by the specific verification check for it, and it is the single reason to budget most of 8b on the prompt rather than the plumbing.
10. **Autonomous memory writes need an undo, and now have one.** `omen memory forget <ref>` is not optional polish: a bad learned memory silently degrades every future scan, and without removal the only recovery is rebuilding the ledger.
11. **Constructed git history is a hidden cost.** Phase 8b needs real commits in `demo_repo/` — a plausible fix commit plus decoys the Sifter should reject. Roughly 15 minutes of scripted commits, easy to forget when estimating.
12. **Dependency weight on the 5050.** `google-adk` + `litellm` + `chromadb` is a big tree and Windows wheel resolution surprises. Install pinned on both machines in phase 0; a fresh `pip install` on demo morning is the most avoidable way to lose this.
13. **Blackwell (sm_120).** Ollama and current llama.cpp are fine. `transformers` + `bitsandbytes` is a wheel-compatibility swamp — don't go there.
