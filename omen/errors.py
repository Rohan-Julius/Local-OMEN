"""Failures a user can act on, each with a message fit to print as one line
(production plan Phase 5, "graceful errors"). `omen.cli.main` prints the
message and exits 3 (runtime or model error); everything else is a bug and
keeps the generic `error: <Type>: <message>` line.

`classify` turns the raw exception a library raised (urllib, httpx, the
ollama client, litellm through ADK) into one of these, so every code path
reports "Ollama is down" or "model missing" the same way. It matches by
type name for libraries this module should not import (litellm, httpx).
"""
from __future__ import annotations

import asyncio
import re
import urllib.error

from omen import config


class OmenError(Exception):
    """A failure whose message is the whole story."""


class OllamaUnavailable(OmenError):
    def __init__(self, host: str | None = None):
        self.host = host or config.OLLAMA_HOST
        super().__init__(f"Ollama is not reachable at {self.host}; start it with `ollama serve`")


class ModelMissing(OmenError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"model {name} not found; run `ollama pull {name}`")


class SchemaInvalid(OmenError):
    """The model's structured reply did not match the schema, twice."""


class LLMTimeout(OmenError):
    def __init__(self, seconds: float | None = None, what: str = "model call"):
        self.seconds = seconds
        after = f" after {seconds:g}s" if seconds else ""
        super().__init__(
            f"{what} timed out{after}; the model may still be loading or is too slow for this profile "
            "(see --profile and OMEN_PROFILE)"
        )


_CONNECTION_NAMES = {"ConnectError", "ConnectTimeout", "APIConnectionError", "NewConnectionError", "ConnectionRefusedError"}
_TIMEOUT_NAMES = {"ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException", "APITimeoutError", "Timeout"}
_MISSING_NAMES = {"ResponseError", "NotFoundError"}
_MODEL_NOT_FOUND = re.compile(r"model\s+[\"']?([^\"'\s,}]+)[\"']?\s+not found", re.IGNORECASE)


def _chain(exc: BaseException, limit: int = 6):
    seen = 0
    while exc is not None and seen < limit:
        yield exc
        exc = exc.__cause__ or exc.__context__
        seen += 1


def classify(exc: BaseException, model: str | None = None) -> OmenError | None:
    """The OmenError `exc` amounts to, or None if it is not one of the
    known operational failures. `model` is named in a "model not found"
    message when the exception itself does not say which model."""
    for e in _chain(exc):
        name = type(e).__name__
        if isinstance(e, OmenError):
            return e
        if isinstance(e, urllib.error.HTTPError):
            if e.code == 404:
                return ModelMissing(_missing_model(str(e), model))
            continue
        if name in _MISSING_NAMES:
            status = getattr(e, "status_code", None)
            if status == 404 or _MODEL_NOT_FOUND.search(str(e)):
                return ModelMissing(_missing_model(str(e), model))
        if isinstance(e, ConnectionError) or isinstance(e, urllib.error.URLError) or name in _CONNECTION_NAMES:
            return OllamaUnavailable()
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)) or name in _TIMEOUT_NAMES:
            return LLMTimeout()
    return None


def _missing_model(text: str, fallback: str | None) -> str:
    m = _MODEL_NOT_FOUND.search(text)
    return m.group(1) if m else (fallback or config.LLM_MODEL)
