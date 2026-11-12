"""Warm-up (audit B27): one throwaway generation before a command's first
real LLM call.

Why. CLAUDE.md records that the first inference call after an Ollama
restart can hard-crash the CUDA backend (`llama-server process has
terminated ... CUDA error: shared object initialization failed`) and then
succeed on an immediate retry. Taking that crash on a throwaway call, with
one retry, keeps it away from a real Triage or Investigator turn. It also
loads the model, so the first real call is not charged the load time, and
`/api/ps` can then say whether the model sits on a GPU (see profiles.py).

The warm-up uses the same `num_gpu`/`num_ctx` as real calls: different
options make Ollama reload the model.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import ollama

from omen import config, errors, profiles

log = logging.getLogger("omen.warmup")


def warm_up(
    client=None,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = 1,
) -> float:
    """Run one tiny generation; return the seconds it took. A failure that
    is not "Ollama down" or "model missing" is retried `retries` times
    (the known CUDA first-call crash); those two cannot be fixed by a retry
    and are raised at once, as friendly errors."""
    client = client or ollama.Client(host=config.OLLAMA_HOST, timeout=profiles.WARMUP_TIMEOUT)
    start = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            client.chat(
                model=config.LLM_MODEL,
                messages=[{"role": "user", "content": "Reply with the single word: ready"}],
                think=False,
                options={
                    "num_gpu": config.NUM_GPU,
                    "num_ctx": config.NUM_CTX,
                    "num_predict": 1,
                    "temperature": config.TEMPERATURE,
                },
            )
            return time.perf_counter() - start
        except Exception as e:
            friendly = errors.classify(e, config.LLM_MODEL)
            if isinstance(friendly, (errors.OllamaUnavailable, errors.ModelMissing)):
                raise friendly from e
            if attempt >= retries:
                raise friendly or e
            log.warning("warm-up call failed (%s: %s); retrying once", type(e).__name__, e)
            sleep(1.0)
    raise AssertionError("unreachable")  # pragma: no cover
