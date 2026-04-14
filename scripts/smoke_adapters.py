"""Smoke-test each adapter against a real query.

Run: .venv/bin/python scripts/smoke_adapters.py [query]

Not a proper test — just a diagnostic. It hits the live services so it's
network-dependent and rate-sensitive; don't run in a loop.
"""

from __future__ import annotations

import asyncio
import sys

from zakupator.adapters.auchan import AuchanAdapter
from zakupator.adapters.metro import MetroAdapter
from zakupator.adapters.vkusvill import VkusVillAdapter
from zakupator.models import Address


MSK_DEFAULT = Address(
    label="Москва (default)",
    text="Москва, центр",
    lat=55.7558,
    lon=37.6173,
)


async def run(query: str) -> None:
    adapters = {
        "ВкусВилл": VkusVillAdapter(),
        "Ашан": AuchanAdapter(),
        "Metro": MetroAdapter(),
    }
    try:
        for name, adapter in adapters.items():
            print(f"\n=== {name} ===")
            result = await adapter.search(query, MSK_DEFAULT, limit=3)
            if result.error:
                print(f"  ! error: {result.error}")
                continue
            if not result.offers:
                print("  (no offers)")
                continue
            for offer in result.offers:
                price_note = f"{offer.price}"
                if offer.price_original and offer.price_original > offer.price:
                    price_note += f" (был {offer.price_original})"
                amount = f" / {offer.amount_unit}" if offer.amount_unit else ""
                stock = "" if offer.in_stock else " [нет в наличии]"
                print(f"  · {offer.title[:70]}")
                print(f"    {price_note} ₽{amount}{stock}")
                if offer.deep_link:
                    print(f"    {offer.deep_link}")
    finally:
        for adapter in adapters.values():
            await adapter.close()


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "молоко простоквашино"
    asyncio.run(run(query))
