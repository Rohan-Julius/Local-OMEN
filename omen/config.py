"""The only place models are named (PLAN.md chokepoint table: "12B -> E4B
on OOM" should be a one-line change here). Also owns the Ollama runtime
flags discovered during Phase 0 that are load-bearing for latency and
correctness on this specific GPU — see CLAUDE.md "Critical Ollama/GPU quirks".
(The original Phase 0 measurements are in git history: README.md at commit 65f46e6.)
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

LLM_MODEL = "gemma4:12b-it-q4_K_M"
EMBED_MODEL = "embeddinggemma"

# One Ollama address for every caller (audit B16): the embedding call
# (vectors.py), the direct runner's ollama.Client (runners.py) and LiteLLM's
# api_base (agents.py) all read config.OLLAMA_HOST at call time. Ollama's own
# OLLAMA_HOST convention allows "host:port" without a scheme and the server
# bind address "0.0.0.0", so the value is normalized to a URL a client can use.
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_PORT = 11434


def normalize_ollama_host(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return DEFAULT_OLLAMA_HOST
    if "://" not in value:
        value = f"http://{value}"
    parts = urlsplit(value)
    host = parts.hostname or "localhost"
    if host == "0.0.0.0":
        host = "127.0.0.1"
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port or DEFAULT_OLLAMA_PORT
    return f"{parts.scheme}://{host}:{port}{parts.path.rstrip('/')}"


OLLAMA_HOST = normalize_ollama_host(os.environ.get("OLLAMA_HOST"))

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

# Store location. Precedence: the CLI's --store flag (set_store_dir), then
# the OMEN_HOME environment variable, then ./omen_store relative to the
# current directory (the original default). Other modules read these three
# names from this module at call time, never as import-time copies.
DEFAULT_STORE_DIR = Path("omen_store")
STORE_DIR: Path
DB_PATH: Path
CHROMA_PATH: Path


def set_store_dir(path: str | os.PathLike | None) -> Path:
    global STORE_DIR, DB_PATH, CHROMA_PATH
    STORE_DIR = Path(path) if path else DEFAULT_STORE_DIR
    DB_PATH = STORE_DIR / "omen.db"
    CHROMA_PATH = STORE_DIR / "chroma"
    return STORE_DIR


set_store_dir(os.environ.get("OMEN_HOME"))
CHROMA_COLLECTION = "incidents"

# Librarian (Phase 3). SIMILARITY_THRESHOLD chosen by Phase 4's fixture
# sweep: 4/4 true positives survive, 2/4 hard negatives survive (the
# PLAN.md acceptance bar). The sweep is in docs/PRODUCTION_PLAN.md ("Phase 0
# calibration sweep") and is reproduced by `omen calibrate`; the margin
# (0.343 strongest rejected hard negative vs 0.360 weakest true positive) is
# real but tight. The sweep is over fixtures/calibration.yaml (the tuning
# set); results on fixtures/heldout.yaml are reported by `omen eval`.
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
# The GPU profile's per-investigation deadline (profiles.GPU). Other limits
# (per-LLM-call timeout, tool-time cap, CPU values) live in profiles.py.
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

# omen scan (production plan Phase 5; PLAN.md caps). Chunks beyond these are
# never silently dropped: they are reported and stored in `skipped_chunks`.
MAX_GATED = 8  # chunks that go to Triage, highest similarity first
MAX_POSITIVES = 2  # Triage-positive chunks that get an Investigator run
