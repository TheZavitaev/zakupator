"""Fan-out search across all configured service adapters.

Runs queries to every adapter in parallel and returns a list of SearchResults.
Individual failures don't poison the batch — each adapter contributes a result
that either carries offers or an error string.

The bot talks to this module, not to adapters directly.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from zakupator.adapters.auchan import AuchanAdapter
from zakupator.adapters.base import ServiceAdapter
from zakupator.adapters.metro import MetroAdapter
from zakupator.adapters.vkusvill import VkusVillAdapter
from zakupator.models import Address, SearchResult, Service
from zakupator.response_cache import ResponseCache


def build_default_adapters() -> list[ServiceAdapter]:
    """Instantiate all Tier 0 adapters with their own HTTP clients.

    Each adapter owns its own httpx.AsyncClient for header isolation —
    VkusVill wants text/html, Metro wants an Origin header, Auchan is
    relaxed. Sharing one client across them would force a common header
    profile which we'd rather avoid.
    """
    return [VkusVillAdapter(), AuchanAdapter(), MetroAdapter()]


class SearchEngine:
    """Holds a pool of adapters and fans out queries across them.

    Optionally sits behind a `ResponseCache` so repeated identical queries
    within a short window don't hit the network. Cache is per-engine so
    tests can pass a fresh one in.
    """

    def __init__(
        self,
        adapters: list[ServiceAdapter] | None = None,
        response_cache: ResponseCache | None = None,
    ) -> None:
        self._adapters = adapters or build_default_adapters()
        self._cache = response_cache if response_cache is not None else ResponseCache()
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "SearchEngine":
        self._stack = AsyncExitStack()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def cache(self) -> ResponseCache:
        return self._cache

    async def search(
        self,
        query: str,
        address: Address,
        *,
        limit_per_service: int = 3,
        timeout: float = 12.0,
    ) -> list[SearchResult]:
        """Run `search(query, ...)` on every adapter in parallel.

        Any cached-fresh results are returned immediately without touching
        the network. The remaining adapters get a per-batch timeout so one
        slow service doesn't stall the user's Telegram response.
        """
        # Resolve which adapters we actually need to call. Cached hits
        # contribute their results directly to the final list.
        cached: list[SearchResult] = []
        to_fetch: list[ServiceAdapter] = []
        for adapter in self._adapters:
            hit = self._cache.get(adapter.service, query, limit_per_service)
            if hit is not None:
                cached.append(hit)
            else:
                to_fetch.append(adapter)

        fresh: list[SearchResult] = []
        if to_fetch:
            tasks = [
                asyncio.create_task(
                    self._safe_search(a, query, address, limit_per_service),
                    name=f"search/{a.service.value}",
                )
                for a in to_fetch
            ]
            done, pending = await asyncio.wait(tasks, timeout=timeout)

            for task in done:
                result = task.result()
                fresh.append(result)
                if not result.error and result.offers:
                    self._cache.put(
                        result.service, query, limit_per_service, result
                    )

            # Cancel stragglers and mark them as timeouts.
            for task in pending:
                task.cancel()
                service = self._service_from_task(task, to_fetch)
                fresh.append(
                    SearchResult(query=query, service=service, error="timeout")
                )

        # Combine cached + fresh, then re-order to match the adapter list.
        all_results = cached + fresh
        order = [a.service for a in self._adapters]
        all_results.sort(
            key=lambda r: order.index(r.service) if r.service in order else 999
        )
        return all_results

    @staticmethod
    async def _safe_search(
        adapter: ServiceAdapter,
        query: str,
        address: Address,
        limit: int,
    ) -> SearchResult:
        try:
            return await adapter.search(query, address, limit=limit)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return SearchResult(
                query=query,
                service=adapter.service,
                error=f"unhandled: {type(e).__name__}: {str(e)[:80]}",
            )

    def _service_from_task(
        self, task: asyncio.Task, candidates: list[ServiceAdapter]
    ) -> Service:
        name = task.get_name() or ""
        prefix = "search/"
        if name.startswith(prefix):
            value = name[len(prefix):]
            try:
                return Service(value)
            except ValueError:
                pass
        # Fallback — shouldn't happen in practice.
        return candidates[0].service if candidates else self._adapters[0].service

    async def close(self) -> None:
        for adapter in self._adapters:
            try:
                await adapter.close()
            except Exception:
                pass
