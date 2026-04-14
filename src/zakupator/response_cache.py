"""Short-lived cache of successful SearchResults per (service, query).

Purpose: a user who runs `/search молоко` then `/compare молоко` a minute
later shouldn't trigger three fresh HTTP calls again. Also cushions transient
service blips — a result that worked five seconds ago is very likely still
accurate.

Design notes:
- Only *successful* (no error) results go into the cache. We don't want to
  remember a 503 and keep showing "service down" after it recovered.
- Key is (service, normalized_query, limit). Normalization is conservative:
  case-fold + strip + collapse internal whitespace. Heavier normalization
  would risk merging different queries together.
- TTL is short by default (5 min). Prices in grocery apps don't change that
  fast but they DO change within an hour.
- This cache sits in front of adapters, inside SearchEngine. Adapters don't
  know it exists.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from zakupator.constants import RESPONSE_CACHE_MAX_SIZE, RESPONSE_CACHE_TTL_SECONDS
from zakupator.models import SearchResult, Service

_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize(query: str) -> str:
    return _WHITESPACE_RUN.sub(" ", query.casefold().strip())


@dataclass(frozen=True)
class _Entry:
    result: SearchResult
    expires_at: float


class ResponseCache:
    def __init__(
        self,
        max_size: int = RESPONSE_CACHE_MAX_SIZE,
        ttl_seconds: float = RESPONSE_CACHE_TTL_SECONDS,
    ) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._store: OrderedDict[tuple[str, str, int], _Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, service: Service, query: str, limit: int) -> SearchResult | None:
        key = (service.value, _normalize(query), limit)
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        if time.monotonic() >= entry.expires_at:
            self._store.pop(key, None)
            self.misses += 1
            return None
        # Refresh LRU order on hit.
        self._store.move_to_end(key)
        self.hits += 1
        return entry.result

    def put(self, service: Service, query: str, limit: int, result: SearchResult) -> None:
        """Store a successful result. No-op for errored or empty results.

        Empty results aren't cached because the user may retry with a
        slightly different query and we don't want to bake in misses.
        """
        if result.error or not result.offers:
            return
        key = (service.value, _normalize(query), limit)
        self._store[key] = _Entry(result=result, expires_at=time.monotonic() + self._ttl)
        self._store.move_to_end(key)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0
