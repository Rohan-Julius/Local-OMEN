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

# Gemma 4's default "thinking" mode costs ~8x latency for no observed
# verdict-quality difference on structured judging prompts (56s vs 7s,
# ~560 vs ~56 tokens). Disabled by default for the fast structured roles
# (Triage, Adjudicator); the Investigator may override to True for a
# richer live reasoning trace, at the accepted latency cost.
THINK_DEFAULT = False

STORE_DIR = Path("omen_store")
DB_PATH = STORE_DIR / "omen.db"
CHROMA_PATH = STORE_DIR / "chroma"
CHROMA_COLLECTION = "incidents"
