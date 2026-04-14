"""Retry helper for outbound HTTP calls.

Adapters use `fetch_with_retry` instead of calling `client.request()` directly
so transient failures (timeouts, connection resets, 5xx) get a couple of quick
retries before propagating. Client errors (4xx, except 429) are not retried —
they won't get better and we'd just waste time.

Keep this file tiny and dependency-free. It's the only place retry logic
lives so adapters stay boring.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from zakupator.constants import RETRY_BACKOFF_SECONDS, RETRY_MAX_ATTEMPTS

logger = logging.getLogger(__name__)


class FetchFailure(StrEnum):
    """Closed set of reasons `fetch_with_retry` may fail.

    Values are the short stable tags the bot uses to pick user copy.
    `HTTP` is special: when paired with a status code, the exposed tag
    widens to e.g. "http 503" so the humanizer can distinguish between
    retryable overloads and persistent bad requests.
    """

    HTTP = "http"
    NETWORK = "network"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = RETRY_MAX_ATTEMPTS
    # Delays between attempts in seconds. len == max_attempts - 1.
    # After final attempt we give up, so there's no delay after it.
    backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS


DEFAULT_POLICY = RetryPolicy()

# Status codes that are worth retrying. 429 is rate-limited (server is
# telling us "slow down"). 5xx are server bugs or overloads — usually
# transient. 408 is request timeout. Everything else: no retry.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class FetchError(Exception):
    """Unified error bubble for adapters.

    `reason` is a closed enum of failure categories. `status` is set
    only when `reason is FetchFailure.HTTP`.
    """

    def __init__(
        self,
        reason: FetchFailure,
        *,
        status: int | None = None,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.status = status
        self.detail = detail
        super().__init__(reason.value if not detail else f"{reason.value}: {detail}")

    @property
    def tag(self) -> str:
        """Stable short tag suitable for SearchResult.error.

        Examples: "http 503", "http 429", "network", "timeout".
        The bot's `_humanize_error` knows how to map these into user text.
        """
        if self.reason is FetchFailure.HTTP and self.status is not None:
            return f"http {self.status}"
        return self.reason.value


async def fetch_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    **kwargs: Any,
) -> httpx.Response:
    """Issue an HTTP request with retries on transient failures.

    Returns the successful `httpx.Response` on success. Raises `FetchError`
    with a stable `reason` tag on persistent failure so callers can render
    a consistent message.

    Non-retryable HTTP errors (most 4xx) are returned as-is — the caller
    decides how to handle them (often "404 means empty result", which
    shouldn't be a retry or an exception).
    """
    last_exc: Exception | None = None
    for attempt in range(policy.max_attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
            # Transport-level failure — always worth retrying.
            last_exc = e
            logger.debug(
                "fetch transport error attempt %d/%d: %s",
                attempt + 1,
                policy.max_attempts,
                type(e).__name__,
            )
        except httpx.HTTPError as e:
            # Other httpx errors: protocol violations, decode errors etc.
            # These are not typically transient, so don't retry.
            raise FetchError(FetchFailure.NETWORK, detail=str(e)[:80]) from e
        else:
            # Got a response. Retry only if the status is in our retryable set.
            if response.status_code in _RETRYABLE_STATUS:
                last_exc = FetchError(
                    FetchFailure.HTTP,
                    status=response.status_code,
                    detail=f"status {response.status_code}",
                )
                logger.debug(
                    "fetch retryable http %d attempt %d/%d",
                    response.status_code,
                    attempt + 1,
                    policy.max_attempts,
                )
            else:
                return response

        # If we're out of attempts, stop — we'll raise below.
        if attempt >= policy.max_attempts - 1:
            break

        # Otherwise wait the scheduled delay and try again. Delays shorter
        # than max_attempts-1 fall back to the last one (defensive default).
        try:
            delay = policy.backoff[attempt]
        except IndexError:
            delay = policy.backoff[-1] if policy.backoff else 1.0
        await asyncio.sleep(delay)

    # All attempts exhausted.
    if isinstance(last_exc, FetchError):
        raise last_exc
    raise FetchError(
        FetchFailure.NETWORK,
        detail=type(last_exc).__name__ if last_exc else "unknown",
    )
