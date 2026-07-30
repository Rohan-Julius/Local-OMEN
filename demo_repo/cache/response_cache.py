"""Response caching for the public status page — the content is
identical for every visitor, so it's safe to cache verbatim."""
import time

_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 60


def get_cached_response(path: str) -> str | None:
    entry = _cache.get(path)
    if entry is None:
        return None
    body, cached_at = entry
    if time.time() - cached_at > _CACHE_TTL_SECONDS:
        del _cache[path]
        return None
    return body


def cache_response(path: str, body: str) -> None:
    _cache[path] = (body, time.time())


def render_status_page() -> str:
    cached = get_cached_response("/status")
    if cached is not None:
        return cached
    body = _build_status_body()
    cache_response("/status", body)
    return body


def _build_status_body() -> str:
    return "<html><body>All systems operational.</body></html>"
