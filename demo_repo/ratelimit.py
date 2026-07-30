"""Simple per-client request rate limiting to help prevent API abuse."""
import time

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 100
_TRUSTED_PROXIES = {"10.0.0.1", "10.0.0.2"}

_request_log: dict[str, list[float]] = {}


def _client_key(headers: dict, remote_addr: str) -> str:
    if remote_addr in _TRUSTED_PROXIES and "X-Forwarded-For" in headers:
        return headers["X-Forwarded-For"]
    return remote_addr


def is_rate_limited(headers: dict, remote_addr: str) -> bool:
    key = _client_key(headers, remote_addr)
    now = time.time()
    log = _request_log.setdefault(key, [])
    log[:] = [t for t in log if now - t < _WINDOW_SECONDS]
    if len(log) >= _MAX_REQUESTS:
        return True
    log.append(now)
    return False
