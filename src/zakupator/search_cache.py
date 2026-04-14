"""Short-lived in-memory cache of search results, keyed by a short token.

Purpose: when the user clicks an "Add to cart" button under a /search message,
we need to know which specific Offer they picked — with its exact title,
price, deep_link etc frozen at display time. We *could* re-run the query but
that risks a price race (the user saw 100 ₽, by the time they click it's 110 ₽
— unpleasant). Instead we freeze the displayed list for a short window.

The cache is process-local. Since we're running a single bot instance with
long-polling, this is fine. When we go multi-worker this will need Redis
or similar.
"""

from __future__ import annotations

import secrets
import string
import time
from collections import OrderedDict
from dataclasses import dataclass

from zakupator.constants import (
    SEARCH_CACHE_MAX_SIZE,
    SEARCH_CACHE_TOKEN_LENGTH,
    SEARCH_CACHE_TTL_SECONDS,
)
from zakupator.models import Offer, SearchResult, Service


@dataclass(frozen=True)
class CachedSearch:
    token: str
    query: str
    # One ordered list of (service, offer) so callback indices are stable.
    flat_offers: tuple[Offer, ...]
    created_at: float


class SearchCache:
    """Bounded LRU of recent search results with TTL."""

    _ALPHABET = string.ascii_lowercase + string.digits

    def __init__(
        self,
        max_size: int = SEARCH_CACHE_MAX_SIZE,
        ttl_seconds: float = SEARCH_CACHE_TTL_SECONDS,
    ) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, CachedSearch] = OrderedDict()

    def put(self, query: str, results: list[SearchResult]) -> CachedSearch:
        """Store a set of search results and return the cache entry.

        `flat_offers` is ordered by the result list order, which in turn is
        ordered by service (stable via SearchEngine). Callback data can
        reference offers by their flat index.
        """
        flat: list[Offer] = []
        for result in results:
            if result.error:
                continue
            flat.extend(result.offers)

        token = self._unique_token()
        entry = CachedSearch(
            token=token,
            query=query,
            flat_offers=tuple(flat),
            created_at=time.monotonic(),
        )
        self._store[token] = entry
        self._evict()
        return entry

    def get(self, token: str) -> CachedSearch | None:
        entry = self._store.get(token)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self._ttl:
            self._store.pop(token, None)
            return None
        # Refresh LRU order — user just touched this entry.
        self._store.move_to_end(token)
        return entry

    def _unique_token(self) -> str:
        # 6 chars from 36 alphabet → ~2 billion space. Collisions are not
        # a safety issue (we'd only overwrite an older entry), but we still
        # retry a few times to keep them rare. Using secrets over random
        # is free and silences B311 — the token IS user-facing in callback
        # data so unpredictability is a mild plus.
        for _ in range(5):
            token = "".join(
                secrets.choice(self._ALPHABET) for _ in range(SEARCH_CACHE_TOKEN_LENGTH)
            )
            if token not in self._store:
                return token
        # Last resort: just overwrite.
        return "".join(secrets.choice(self._ALPHABET) for _ in range(SEARCH_CACHE_TOKEN_LENGTH))

    def _evict(self) -> None:
        # Drop oldest entries when over capacity. TTL eviction is lazy on get.
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)


# Compact single-letter service codes for callback_data (stay under the
# 64-byte Telegram limit with room to spare).
SERVICE_CODE: dict[Service, str] = {
    Service.VKUSVILL: "v",
    Service.AUCHAN: "a",
    Service.METRO: "m",
}
CODE_TO_SERVICE: dict[str, Service] = {v: k for k, v in SERVICE_CODE.items()}
