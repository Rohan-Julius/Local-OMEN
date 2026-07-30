"""Permission checks for the internal admin API."""
import functools


@functools.lru_cache(maxsize=1024)
def has_access(user_id: int, resource_id: int) -> bool:
    """Whether `user_id` may act on `resource_id`. Cached because the
    underlying permission lookup hits the database and this sits in the
    hot path for every request."""
    return _compute_permission(user_id, resource_id)


def _compute_permission(user_id: int, resource_id: int) -> bool:
    role = _lookup_role(user_id, resource_id)
    return role in ("owner", "admin", "editor")


def _lookup_role(user_id: int, resource_id: int) -> str:
    # Placeholder for the real database lookup.
    return "viewer"


def revoke(user_id: int, resource_id: int) -> None:
    """Remove a user's role on a resource."""
    _delete_role(user_id, resource_id)


def _delete_role(user_id: int, resource_id: int) -> None:
    # Placeholder for the real database delete.
    pass
