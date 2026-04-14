"""SearchCache — LRU with TTL."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import patch

import pytest

from zakupator.models import Offer, SearchResult, Service
from zakupator.search_cache import SearchCache


def _offer(service: Service = Service.VKUSVILL, title: str = "x", price: str = "1") -> Offer:
    return Offer(
        service=service,
        product_id=title,
        title=title,
        price=Decimal(price),
    )


def _result(service: Service, *offers: Offer) -> SearchResult:
    return SearchResult(query="q", service=service, offers=list(offers))


def test_put_returns_entry_with_token_and_flat_offers():
    cache = SearchCache()
    entry = cache.put(
        "q",
        [
            _result(Service.VKUSVILL, _offer(title="a")),
            _result(Service.AUCHAN, _offer(Service.AUCHAN, "b"), _offer(Service.AUCHAN, "c")),
        ],
    )
    assert len(entry.token) == 6
    assert entry.query == "q"
    assert len(entry.flat_offers) == 3
    assert entry.flat_offers[0].title == "a"
    assert entry.flat_offers[1].title == "b"
    assert entry.flat_offers[2].title == "c"


def test_put_skips_errored_results():
    cache = SearchCache()
    entry = cache.put(
        "q",
        [
            _result(Service.VKUSVILL, _offer(title="ok")),
            SearchResult(query="q", service=Service.AUCHAN, error="http 500"),
        ],
    )
    assert len(entry.flat_offers) == 1
    assert entry.flat_offers[0].title == "ok"


def test_get_returns_entry_until_ttl_expires():
    cache = SearchCache(ttl_seconds=0.05)
    entry = cache.put("q", [_result(Service.VKUSVILL, _offer(title="a"))])
    assert cache.get(entry.token) is not None
    time.sleep(0.06)
    assert cache.get(entry.token) is None


def test_get_unknown_token_returns_none():
    cache = SearchCache()
    assert cache.get("nope00") is None


def test_lru_evicts_oldest_when_over_capacity():
    cache = SearchCache(max_size=3)
    tokens = [
        cache.put(f"q{i}", [_result(Service.VKUSVILL, _offer(title=str(i)))]).token
        for i in range(4)
    ]
    # First one is now evicted, the other three are alive.
    assert cache.get(tokens[0]) is None
    for t in tokens[1:]:
        assert cache.get(t) is not None


def test_get_refreshes_lru_order():
    cache = SearchCache(max_size=3)
    t0 = cache.put("q0", [_result(Service.VKUSVILL, _offer(title="0"))]).token
    t1 = cache.put("q1", [_result(Service.VKUSVILL, _offer(title="1"))]).token
    t2 = cache.put("q2", [_result(Service.VKUSVILL, _offer(title="2"))]).token
    # Touch t0 — it becomes the most recently used.
    assert cache.get(t0) is not None
    # Adding a fourth should evict t1 (oldest not-touched), not t0.
    cache.put("q3", [_result(Service.VKUSVILL, _offer(title="3"))])
    assert cache.get(t0) is not None, "recently-accessed token must survive"
    assert cache.get(t1) is None, "oldest unused token must have been evicted"
    assert cache.get(t2) is not None
