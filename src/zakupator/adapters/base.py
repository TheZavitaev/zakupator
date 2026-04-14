"""Base interface every delivery-service adapter implements.

The bot doesn't care whether data comes from a JSON API, a headless browser,
or a cached fixture - it just calls `search(query, address)` and gets offers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from zakupator.models import Address, SearchResult, Service


class ServiceAdapter(ABC):
    service: Service

    @abstractmethod
    async def search(self, query: str, address: Address, limit: int = 5) -> SearchResult:
        """Search products by free-text query, scoped to the given address."""

    async def close(self) -> None:
        """Release any held resources (http clients, browser contexts)."""
