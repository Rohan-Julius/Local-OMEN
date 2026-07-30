from ratelimit import is_rate_limited


def test_allows_requests_under_the_limit():
    assert is_rate_limited({}, "203.0.113.5") is False


def test_blocks_requests_once_over_the_limit():
    for _ in range(100):
        is_rate_limited({}, "203.0.113.5")
    assert is_rate_limited({}, "203.0.113.5") is True


def test_ignores_forwarded_for_from_an_untrusted_client():
    """The header only takes effect from a trusted proxy address — an
    ordinary client can't set it to reset its own limit."""
    headers = {"X-Forwarded-For": "1.2.3.4"}
    for _ in range(100):
        is_rate_limited(headers, "203.0.113.5")
    # Same untrusted remote_addr, header ignored -> same bucket, now limited.
    assert is_rate_limited({}, "203.0.113.5") is True
