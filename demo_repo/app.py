"""Route handlers for the internal admin API — a small illustrative
service, not a real framework app."""
from permissions import has_access, revoke
from ratelimit import is_rate_limited
from cache.response_cache import render_status_page


def handle_resource_request(user_id: int, resource_id: int, headers: dict, remote_addr: str) -> str:
    if is_rate_limited(headers, remote_addr):
        return "429 Too Many Requests"
    if not has_access(user_id, resource_id):
        return "403 Forbidden"
    return f"200 OK: resource {resource_id}"


def handle_revoke_request(user_id: int, resource_id: int) -> str:
    revoke(user_id, resource_id)
    return "204 No Content"


def handle_status_request() -> str:
    return render_status_page()
