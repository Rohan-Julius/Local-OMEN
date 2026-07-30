"""The only place models are named (PLAN.md chokepoint table: "12B -> E4B
on OOM" should be a one-line change here). Also owns the Ollama runtime
flags discovered during Phase 0 that are load-bearing for latency and
correctness on this specific GPU — see README.md "Phase 0 results".
"""
from __future__ import annotations

from pathlib import Path

LLM_MODEL = "gemma4:12b-it-q4_K_M"
EMBED_MODEL = "embeddinggemma"

OLLAMA_HOST = "http://localhost:11434"

# PLAN.md Phase 0c, revised after real measurement on the RTX 5050 (8GB):
# num_gpu forces full GPU residency (without it, Ollama silently splits
# ~30/70 CPU/GPU and drops to ~5 tok/s). num_ctx=8192 does not fit fully on
# GPU on this card; 4096 does.
NUM_GPU = 999
NUM_CTX = 4096

# PLAN.md Verification: "Determinism. Temperature 0; identical Triage
# verdicts across runs." Applied to every role, not just Triage — nothing
# here benefits from sampling noise.
TEMPERATURE = 0

# Gemma 4's default "thinking" mode costs ~8x latency for no observed
# verdict-quality difference on structured judging prompts (56s vs 7s,
# ~560 vs ~56 tokens). Disabled by default for the fast structured roles
# (Triage, Adjudicator); the Investigator may override to True for a
# richer live reasoning trace, at the accepted latency cost.
THINK_DEFAULT = False
INVESTIGATOR_THINK_DEFAULT = True
ARCHIVIST_THINK_DEFAULT = True

STORE_DIR = Path("omen_store")
DB_PATH = STORE_DIR / "omen.db"
CHROMA_PATH = STORE_DIR / "chroma"
CHROMA_COLLECTION = "incidents"

# Librarian (Phase 3). SIMILARITY_THRESHOLD chosen by Phase 4's fixture
# sweep: 4/4 true positives survive, 2/4 hard negatives survive (the
# PLAN.md acceptance bar) — see README.md "Phase 4 results" for the sweep
# and why the margin (0.344 rejected vs 0.360 weakest true positive) is
# real but tight.
EMBED_BATCH_SIZE = 32
QUERY_N_RESULTS = 6  # variant-level; collapsed to incidents before capping
TOP_K_INCIDENTS = 3
SIMILARITY_THRESHOLD = 0.35

# Memory ref format (Phase 5+): OMEN-001, OMEN-002, ... assigned by
# write_incident when the Archivist creates a new entry.
REF_PREFIX = "OMEN"

# Tool budget (Phase 5). PLAN.md: "an agent loop is an unbounded loop" —
# these three caps are enforced in the orchestrator (tools.py's
# ToolBudget), never merely requested in a prompt.
MAX_TOOL_CALLS = 6
TOOL_WALL_CLOCK_SECONDS = 90
GREP_MAX_HITS = 50
DIFF_LINE_CAP = 300

# omen learn --from-git (Phase 8b). PLAN.md: "--max-commits (default 50)
# bounds the prefilter input; --max-learn (default 5) bounds Archivist
# invocations per run" — a bad memory pollutes every future scan with no
# obvious undo, so these stay conservative even though `omen memory forget`
# is the safety valve.
MAX_COMMITS = 50
MAX_LEARN = 5
