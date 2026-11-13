"""Hardware profiles (production plan Phase 5): what to expect from the
machine Ollama runs the model on, and the time limits and `think` settings
that follow.

Why two profiles. This project's own measurements: on the RTX 5050 a
structured call takes ~7 s with `think=False` (56 s with `think=True`); on
the CPU-only laptop a structured call takes 110-170 s. One set of limits
cannot serve both: the GPU limits refuse tool calls and fail tests on CPU,
and CPU limits would let a hung GPU run wait half an hour.

Three separate limits (audit B5, part 2). Before, one 90 s wall clock
counted model generation too, so a slow model turn caused the next tool
call to be refused:

  llm_call_timeout  one model generation (a Triage, Adjudicator or
                    Investigator turn). Exceeding it ends the loop with
                    stopped_reason="llm_timeout".
  tool_seconds      total time spent INSIDE tools. Tools take milliseconds,
                    except search_memory (one embedding, ~5 s on CPU), so
                    this is a generous cap, not a pacing mechanism.
  deadline_seconds  the whole investigation, model turns included, measured
                    from the first tool call.

Selecting a profile: `--profile auto|gpu|cpu` (global flag), else the
OMEN_PROFILE environment variable, else `auto`. `auto` asks Ollama's
/api/ps after the warm-up call: the model's `size_vram == 0` means CPU.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from typing import Callable

from omen import config

log = logging.getLogger("omen.profiles")

PROFILE_NAMES = ("auto", "gpu", "cpu")


@dataclass(frozen=True)
class Profile:
    name: str
    investigator_think: bool
    archivist_think: bool
    llm_call_timeout: float  # seconds, one model generation
    deadline_seconds: float  # seconds, one whole investigation
    tool_seconds: float  # seconds, total inside tools

    def describe(self) -> str:
        return (
            f"profile {self.name}: investigator think={self.investigator_think}, archivist think={self.archivist_think}, "
            f"llm call timeout {self.llm_call_timeout:g}s, investigation deadline {self.deadline_seconds:g}s, "
            f"tool time cap {self.tool_seconds:g}s"
        )


# The values Omen had before this phase, plus a per-call timeout with 2x
# headroom over the slowest GPU call measured (think=True, ~56 s).
GPU = Profile(
    name="gpu",
    investigator_think=config.INVESTIGATOR_THINK_DEFAULT,
    archivist_think=config.ARCHIVIST_THINK_DEFAULT,
    llm_call_timeout=120.0,
    deadline_seconds=float(config.TOOL_WALL_CLOCK_SECONDS),
    tool_seconds=60.0,
)

# think=False: ~8x latency for no observed quality gain (CLAUDE.md). The
# timeout is 2-3x the slowest single call measured on CPU (~120-250 s); the
# deadline covers a full 6-tool-call investigation (7 model turns, ~120 s
# each) with 2x headroom.
CPU = Profile(
    name="cpu",
    investigator_think=False,
    archivist_think=False,
    llm_call_timeout=600.0,
    deadline_seconds=1800.0,
    tool_seconds=120.0,
)

PROFILES = {"gpu": GPU, "cpu": CPU}

# The first call after Ollama starts also loads the model into memory,
# which on CPU takes minutes.
WARMUP_TIMEOUT = 900.0


def profile_from_ps(ps: dict | None, model: str) -> str | None:
    """Decide "gpu" or "cpu" from an Ollama /api/ps answer, or None if it
    cannot be decided (Ollama did not answer, or `model` is not loaded).

    `size_vram == 0` means no layer is on a GPU: cpu. Any layer on a GPU
    (including a partial offload) is gpu."""
    for entry in (ps or {}).get("models", []):
        if model in (entry.get("name"), entry.get("model")):
            vram = entry.get("size_vram")
            if vram is None:
                return None
            return "cpu" if vram == 0 else "gpu"
    return None


def probe_ps(host: str | None = None, timeout: float = 5.0) -> dict | None:
    host = host or config.OLLAMA_HOST
    try:
        with urllib.request.urlopen(f"{host}/api/ps", timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.debug("could not read %s/api/ps: %s", host, e)
        return None


def requested_name(flag: str | None = None, environ: dict | None = None) -> str:
    """The profile name asked for: flag, else OMEN_PROFILE, else auto."""
    env = (environ if environ is not None else os.environ).get("OMEN_PROFILE")
    name = (flag or env or "auto").strip().lower()
    if name not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {name!r}; choose one of: {', '.join(PROFILE_NAMES)}")
    return name


def resolve(
    flag: str | None = None,
    ps: Callable[[], dict | None] | None = None,
    environ: dict | None = None,
) -> Profile:
    """Turn the request into a Profile. `ps` (default `probe_ps`, looked up
    at call time) is only called for `auto`."""
    name = requested_name(flag, environ)
    if name != "auto":
        return PROFILES[name]
    detected = profile_from_ps((ps or probe_ps)(), config.LLM_MODEL)
    if detected is None:
        log.warning(
            "could not tell whether %s runs on a GPU (Ollama /api/ps did not list it); assuming gpu. "
            "On a CPU-only machine pass --profile cpu or set OMEN_PROFILE=cpu.",
            config.LLM_MODEL,
        )
        detected = "gpu"
    return PROFILES[detected]


_active: Profile | None = None


def set_active(profile: Profile | None) -> None:
    global _active
    _active = profile


def active() -> Profile:
    """The profile in force: the one the CLI resolved, else OMEN_PROFILE if
    it names gpu or cpu (so tests and library users can opt in with the
    environment alone), else gpu."""
    if _active is not None:
        return _active
    env = (os.environ.get("OMEN_PROFILE") or "").strip().lower()
    return PROFILES.get(env, GPU)
