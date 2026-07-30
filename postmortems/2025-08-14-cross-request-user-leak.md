# Postmortem: Wrong user's profile data returned under load

**Date:** 2025-08-14
**Severity:** SEV1
**Service:** `accounts-api` (Python, Flask, gunicorn with the `gthread` worker class)

## Summary

For approximately 40 minutes during a traffic spike, a small percentage of
requests to `GET /api/v1/profile` returned a *different* user's profile
data than the one who made the request. Three customers reported seeing
another account's name, email, and billing address in their own app.

## What happened

`accounts-api`'s authentication middleware resolves the caller's identity
once per request and needs that identity again later in the same request,
inside a view function several layers down the call stack. Rather than
threading the value through function arguments or attaching it to Flask's
per-request `g` object, an earlier engineer had taken a shortcut: the
middleware set a module-level variable,

```python
_current_user = None

def auth_middleware(get_response):
    def middleware(request):
        global _current_user
        _current_user = resolve_user(request)
        return get_response(request)
    return middleware

def get_profile(request):
    # several calls later, in a different module
    return serialize_profile(_current_user)
```

This worked in every environment the team tested in, because local
development and the staging smoke tests only ever handled one request at a
time. In production, gunicorn's `gthread` worker class runs multiple
requests concurrently on the same set of OS threads within one worker
process, and `_current_user` is shared by every thread in that process.

Under the traffic spike, request A's middleware set `_current_user` to
user A, then — before request A's view function ran — request B's
middleware (on another thread, same process) overwrote `_current_user` to
user B. When request A's `get_profile` view finally executed, it read
`_current_user` and serialized user B's data into user A's response.

The window was normally too narrow to hit, which is why this had shipped
months earlier without being noticed — it only became visible once request
volume was high enough to make the interleaving common.

## Impact

3 confirmed customer reports of seeing another account's profile data. No
evidence of write access or payment data exposure, since only the
read-only profile endpoint used this code path.

## Root cause

Per-request state (the authenticated user) was stored in a module-level
global variable instead of something scoped to the individual request.
Under any concurrent execution model — threads, greenlets, or async
tasks — a module-level variable is process-wide shared state, not
request-scoped state, and the assumption "only one request touches this at
a time" silently stops holding the moment the server handles more than one
request per process concurrently.

## Fix

Replaced the module-level `_current_user` with Flask's request-scoped `g`
object (`g.current_user`, set and read within the same request context),
which Flask resets for every request regardless of threading model. Added
a regression test that fires two overlapping requests as different users
on the same worker process and asserts each response contains only its own
user's data.

## Follow-up rule

Any value that is specific to one in-flight request must live in a
mechanism that is actually scoped per request (a request-context object, a
function parameter/return value, or an explicitly request-keyed
`contextvars.ContextVar`) — never in a bare module-level or global
variable, regardless of how safe that looks in a single-request local
test.
