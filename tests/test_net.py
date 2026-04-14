"""fetch_with_retry — retry policy for transient HTTP failures."""

from __future__ import annotations

import httpx
import pytest

from zakupator.net import DEFAULT_POLICY, FetchError, RetryPolicy, fetch_with_retry


def _client_with_script(responses: list) -> tuple[httpx.AsyncClient, list[int]]:
    """Build a client whose handler walks through a scripted list.

    Each element can be:
      * an `httpx.Response` — returned verbatim
      * an `Exception` — raised
    Returns the client and a call counter list (so tests can assert attempts).
    """
    calls: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = calls[0]
        calls[0] += 1
        if idx >= len(responses):
            return httpx.Response(200, content=b"{}")
        item = responses[idx]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


# Fast retry policy for tests so we don't burn a second per test.
FAST_POLICY = RetryPolicy(max_attempts=3, backoff=(0.0, 0.0))


async def test_returns_successful_response_without_retry():
    client, calls = _client_with_script([httpx.Response(200, content=b"ok")])
    try:
        resp = await fetch_with_retry(
            client, "GET", "https://example.com/", policy=FAST_POLICY
        )
        assert resp.status_code == 200
        assert calls[0] == 1
    finally:
        await client.aclose()


async def test_retries_on_503_then_succeeds():
    client, calls = _client_with_script(
        [
            httpx.Response(503, content=b"down"),
            httpx.Response(200, content=b"ok"),
        ]
    )
    try:
        resp = await fetch_with_retry(
            client, "GET", "https://example.com/", policy=FAST_POLICY
        )
        assert resp.status_code == 200
        assert calls[0] == 2, "must have retried once"
    finally:
        await client.aclose()


async def test_retries_on_connect_timeout_then_succeeds():
    client, calls = _client_with_script(
        [
            httpx.ConnectTimeout("boom"),
            httpx.Response(200, content=b"ok"),
        ]
    )
    try:
        resp = await fetch_with_retry(
            client, "GET", "https://example.com/", policy=FAST_POLICY
        )
        assert resp.status_code == 200
        assert calls[0] == 2
    finally:
        await client.aclose()


async def test_gives_up_after_max_attempts_on_persistent_503():
    client, calls = _client_with_script(
        [httpx.Response(503, content=b"down") for _ in range(5)]
    )
    try:
        with pytest.raises(FetchError) as info:
            await fetch_with_retry(
                client, "GET", "https://example.com/", policy=FAST_POLICY
            )
        assert info.value.reason == "http"
        assert info.value.status == 503
        assert calls[0] == 3, "must have tried exactly max_attempts times"
    finally:
        await client.aclose()


async def test_gives_up_after_max_attempts_on_persistent_timeout():
    client, calls = _client_with_script(
        [httpx.ReadTimeout("slow") for _ in range(5)]
    )
    try:
        with pytest.raises(FetchError) as info:
            await fetch_with_retry(
                client, "GET", "https://example.com/", policy=FAST_POLICY
            )
        assert info.value.reason == "network"
        assert calls[0] == 3
    finally:
        await client.aclose()


async def test_does_not_retry_on_404():
    client, calls = _client_with_script([httpx.Response(404, content=b"missing")])
    try:
        resp = await fetch_with_retry(
            client, "GET", "https://example.com/", policy=FAST_POLICY
        )
        # Non-retryable → returned as-is, caller decides what to do.
        assert resp.status_code == 404
        assert calls[0] == 1
    finally:
        await client.aclose()


async def test_retries_on_429_rate_limit():
    client, calls = _client_with_script(
        [
            httpx.Response(429, content=b"too many"),
            httpx.Response(200, content=b"ok"),
        ]
    )
    try:
        resp = await fetch_with_retry(
            client, "GET", "https://example.com/", policy=FAST_POLICY
        )
        assert resp.status_code == 200
        assert calls[0] == 2
    finally:
        await client.aclose()


async def test_default_policy_values_are_sane():
    assert DEFAULT_POLICY.max_attempts >= 2
    assert len(DEFAULT_POLICY.backoff) == DEFAULT_POLICY.max_attempts - 1
    # Backoff should be non-decreasing.
    for a, b in zip(DEFAULT_POLICY.backoff, DEFAULT_POLICY.backoff[1:]):
        assert b >= a
