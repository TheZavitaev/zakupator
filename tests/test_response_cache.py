"""ResponseCache — TTL + LRU cache for successful SearchResults."""

from __future__ import annotations

import time
from decimal import Decimal

from zakupator.models import Offer, SearchResult, Service
from zakupator.response_cache import ResponseCache


def _offer(title: str = "Молоко", price: str = "100") -> Offer:
    return Offer(
        service=Service.VKUSVILL,
        product_id=title,
        title=title,
        price=Decimal(price),
    )


def _good(query: str = "молоко", offers: int = 1) -> SearchResult:
    return SearchResult(
        query=query,
        service=Service.VKUSVILL,
        offers=[_offer(f"m{i}") for i in range(offers)],
    )


def test_miss_on_cold_cache():
    cache = ResponseCache()
    assert cache.get(Service.VKUSVILL, "молоко", 3) is None
    assert cache.misses == 1
    assert cache.hits == 0


def test_put_then_get_returns_same_result():
    cache = ResponseCache()
    result = _good()
    cache.put(Service.VKUSVILL, "молоко", 3, result)
    hit = cache.get(Service.VKUSVILL, "молоко", 3)
    assert hit is result
    assert cache.hits == 1


def test_normalization_is_case_insensitive():
    cache = ResponseCache()
    cache.put(Service.VKUSVILL, "Молоко", 3, _good())
    assert cache.get(Service.VKUSVILL, "молоко", 3) is not None
    assert cache.get(Service.VKUSVILL, "МОЛОКО", 3) is not None


def test_normalization_collapses_whitespace():
    cache = ResponseCache()
    cache.put(Service.VKUSVILL, "молоко  простоквашино", 3, _good())
    assert cache.get(Service.VKUSVILL, "молоко простоквашино", 3) is not None
    assert cache.get(Service.VKUSVILL, "  молоко   простоквашино  ", 3) is not None


def test_different_services_isolated():
    cache = ResponseCache()
    cache.put(Service.VKUSVILL, "молоко", 3, _good())
    assert cache.get(Service.AUCHAN, "молоко", 3) is None


def test_different_limits_isolated():
    cache = ResponseCache()
    cache.put(Service.VKUSVILL, "молоко", 3, _good())
    assert cache.get(Service.VKUSVILL, "молоко", 5) is None


def test_errored_results_not_cached():
    cache = ResponseCache()
    errored = SearchResult(query="q", service=Service.VKUSVILL, error="http 500")
    cache.put(Service.VKUSVILL, "q", 3, errored)
    assert cache.get(Service.VKUSVILL, "q", 3) is None


def test_empty_successful_results_not_cached():
    cache = ResponseCache()
    empty = SearchResult(query="q", service=Service.VKUSVILL, offers=[])
    cache.put(Service.VKUSVILL, "q", 3, empty)
    assert cache.get(Service.VKUSVILL, "q", 3) is None


def test_ttl_expiry():
    cache = ResponseCache(ttl_seconds=0.05)
    cache.put(Service.VKUSVILL, "q", 3, _good())
    assert cache.get(Service.VKUSVILL, "q", 3) is not None
    time.sleep(0.06)
    assert cache.get(Service.VKUSVILL, "q", 3) is None


def test_lru_eviction_when_over_capacity():
    cache = ResponseCache(max_size=2)
    cache.put(Service.VKUSVILL, "a", 3, _good("a"))
    cache.put(Service.VKUSVILL, "b", 3, _good("b"))
    cache.put(Service.VKUSVILL, "c", 3, _good("c"))
    assert cache.get(Service.VKUSVILL, "a", 3) is None
    assert cache.get(Service.VKUSVILL, "b", 3) is not None
    assert cache.get(Service.VKUSVILL, "c", 3) is not None


def test_get_refreshes_lru_order():
    cache = ResponseCache(max_size=2)
    cache.put(Service.VKUSVILL, "a", 3, _good("a"))
    cache.put(Service.VKUSVILL, "b", 3, _good("b"))
    # Touch a — now b is the oldest untouched.
    cache.get(Service.VKUSVILL, "a", 3)
    cache.put(Service.VKUSVILL, "c", 3, _good("c"))
    assert cache.get(Service.VKUSVILL, "a", 3) is not None
    assert cache.get(Service.VKUSVILL, "b", 3) is None


def test_clear_resets_state():
    cache = ResponseCache()
    cache.put(Service.VKUSVILL, "q", 3, _good())
    cache.get(Service.VKUSVILL, "q", 3)
    cache.clear()
    assert cache.get(Service.VKUSVILL, "q", 3) is None
    assert cache.hits == 0
    assert cache.misses == 1
