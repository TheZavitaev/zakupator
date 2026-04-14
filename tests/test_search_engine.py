"""SearchEngine fan-out orchestrator."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from zakupator.adapters.base import ServiceAdapter
from zakupator.models import Address, Offer, SearchResult, Service
from zakupator.search import SearchEngine


class _FakeAdapter(ServiceAdapter):
    def __init__(
        self,
        service: Service,
        *,
        delay: float = 0.0,
        offers: list[Offer] | None = None,
        error: str | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.service = service
        self._delay = delay
        self._offers = offers or []
        self._error = error
        self._raise = raise_exc
        self.closed = False

    async def search(self, query, address, limit=5):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise:
            raise self._raise
        return SearchResult(
            query=query, service=self.service, offers=self._offers, error=self._error
        )

    async def close(self):
        self.closed = True


def _o(service: Service, title: str) -> Offer:
    return Offer(service=service, product_id=title, title=title, price=Decimal("1"))


@pytest.fixture
def address() -> Address:
    return Address(label="t", text="Moscow", lat=55.7, lon=37.6)


async def test_fan_out_collects_all_services(address):
    adapters = [
        _FakeAdapter(Service.VKUSVILL, offers=[_o(Service.VKUSVILL, "a")]),
        _FakeAdapter(Service.AUCHAN, offers=[_o(Service.AUCHAN, "b")]),
        _FakeAdapter(Service.METRO, offers=[_o(Service.METRO, "c")]),
    ]
    async with SearchEngine(adapters) as engine:
        results = await engine.search("x", address)

    assert [r.service for r in results] == [
        Service.VKUSVILL,
        Service.AUCHAN,
        Service.METRO,
    ]
    assert all(r.error is None for r in results)
    assert sum(len(r.offers) for r in results) == 3


async def test_results_ordered_by_adapter_list_regardless_of_finish_time(address):
    # VkusVill takes longer than the others but should still come first
    # because it's registered first in the adapter list.
    adapters = [
        _FakeAdapter(Service.VKUSVILL, delay=0.05, offers=[_o(Service.VKUSVILL, "slow")]),
        _FakeAdapter(Service.AUCHAN, offers=[_o(Service.AUCHAN, "fast")]),
        _FakeAdapter(Service.METRO, offers=[_o(Service.METRO, "fast2")]),
    ]
    async with SearchEngine(adapters) as engine:
        results = await engine.search("x", address)
    assert [r.service for r in results] == [
        Service.VKUSVILL,
        Service.AUCHAN,
        Service.METRO,
    ]


async def test_exception_in_one_adapter_does_not_kill_the_batch(address):
    adapters = [
        _FakeAdapter(Service.VKUSVILL, raise_exc=RuntimeError("boom")),
        _FakeAdapter(Service.AUCHAN, offers=[_o(Service.AUCHAN, "ok")]),
        _FakeAdapter(Service.METRO, offers=[_o(Service.METRO, "ok")]),
    ]
    async with SearchEngine(adapters) as engine:
        results = await engine.search("x", address)
    # VkusVill returns an error SearchResult, the others return offers.
    by_service = {r.service: r for r in results}
    assert by_service[Service.VKUSVILL].error is not None
    assert "RuntimeError" in by_service[Service.VKUSVILL].error
    assert len(by_service[Service.AUCHAN].offers) == 1
    assert len(by_service[Service.METRO].offers) == 1


async def test_timeout_marks_slow_services_as_errored(address):
    adapters = [
        _FakeAdapter(Service.VKUSVILL, delay=10.0),  # will be cancelled
        _FakeAdapter(Service.AUCHAN, offers=[_o(Service.AUCHAN, "fast")]),
        _FakeAdapter(Service.METRO, offers=[_o(Service.METRO, "fast")]),
    ]
    async with SearchEngine(adapters) as engine:
        results = await engine.search("x", address, timeout=0.1)
    by_service = {r.service: r for r in results}
    assert by_service[Service.VKUSVILL].error == "timeout"
    assert len(by_service[Service.AUCHAN].offers) == 1
    assert len(by_service[Service.METRO].offers) == 1


async def test_close_propagates_to_adapters(address):
    adapters = [_FakeAdapter(Service.VKUSVILL) for _ in range(3)]
    engine = SearchEngine(adapters)
    await engine.close()
    assert all(a.closed for a in adapters)


async def test_response_cache_prevents_second_network_call(address):
    from zakupator.response_cache import ResponseCache

    adapter = _FakeAdapter(
        Service.VKUSVILL, offers=[_o(Service.VKUSVILL, "x")]
    )
    # Monkey-patch the adapter to count invocations.
    original_search = adapter.search
    call_count = {"n": 0}

    async def counted_search(*args, **kwargs):
        call_count["n"] += 1
        return await original_search(*args, **kwargs)

    adapter.search = counted_search  # type: ignore[method-assign]

    cache = ResponseCache()
    async with SearchEngine([adapter], response_cache=cache) as engine:
        await engine.search("молоко", address)
        await engine.search("молоко", address)  # should be a cache hit

    assert call_count["n"] == 1, "second identical query must not hit the adapter"
    assert cache.hits == 1
    assert cache.misses == 1


async def test_response_cache_key_ignores_case_and_whitespace(address):
    from zakupator.response_cache import ResponseCache

    adapter = _FakeAdapter(Service.VKUSVILL, offers=[_o(Service.VKUSVILL, "x")])
    cache = ResponseCache()
    async with SearchEngine([adapter], response_cache=cache) as engine:
        r1 = await engine.search("Молоко простоквашино", address)
        r2 = await engine.search("  молоко   простоквашино  ", address)
    assert cache.hits == 1
    assert r1[0].offers[0].title == r2[0].offers[0].title


async def test_errored_results_not_cached(address):
    from zakupator.response_cache import ResponseCache

    adapter = _FakeAdapter(Service.VKUSVILL, error="http 503")
    cache = ResponseCache()
    async with SearchEngine([adapter], response_cache=cache) as engine:
        await engine.search("молоко", address)
        # Second call still hits the adapter because the failure wasn't cached.
        await engine.search("молоко", address)
    assert cache.hits == 0
