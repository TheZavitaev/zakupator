"""Capture real responses from each service as test fixtures.

Hit each service once with a known query, save the raw response body into
tests/fixtures/. Re-run this occasionally (or when a service changes shape)
to refresh the golden data. The fixtures are committed to the repo so tests
don't need network access.

Run:
    .venv/bin/python scripts/capture_fixtures.py

The query "молоко простоквашино" reliably returns multiple products at all
three services, so it's a good baseline.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIX_DIR = ROOT / "tests" / "fixtures"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

QUERY = "молоко простоквашино"


async def capture_vkusvill(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "https://vkusvill.ru/search/",
        params={"type": "products", "q": QUERY},
        headers={"accept": "text/html", "user-agent": UA, "accept-language": "ru"},
        follow_redirects=True,
    )
    resp.raise_for_status()
    (FIX_DIR / "vkusvill_search.html").write_text(resp.text, encoding="utf-8")
    print(f"vkusvill: {len(resp.text)} bytes")


async def capture_auchan(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "https://www.auchan.ru/v3/autohints/",
        params={
            "merchantId": 3,
            "query": QUERY,
            "productsSize": 10,
            "channel": "W",
        },
        headers={"accept": "application/json", "user-agent": UA, "accept-language": "ru"},
    )
    resp.raise_for_status()
    # Pretty-print for readable diffs in the repo.
    data = resp.json()
    (FIX_DIR / "auchan_autohints.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"auchan: {len(resp.content)} bytes")


async def capture_metro(client: httpx.AsyncClient) -> None:
    query_gql = """
    query Search($text: String!, $storeId: Int!, $size: Int!) {
      search(text: $text) {
        products(storeId: $storeId, size: $size) {
          total
          products {
            id article name url slug images
            manufacturer { name }
            stocks {
              store_id eshop_availability value text
              prices { price old_price discount is_promo }
            }
          }
        }
      }
    }
    """
    resp = await client.post(
        "https://supergraph.metro-cc.ru/graphql",
        json={
            "query": query_gql,
            "variables": {"text": QUERY, "storeId": 10, "size": 10},
        },
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": UA,
            "origin": "https://online.metro-cc.ru",
            "referer": "https://online.metro-cc.ru/",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    (FIX_DIR / "metro_graphql.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"metro: {len(resp.content)} bytes")


async def main() -> None:
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    captures = [
        ("vkusvill", capture_vkusvill),
        ("auchan", capture_auchan),
        ("metro", capture_metro),
    ]
    failed: list[tuple[str, str]] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for name, fn in captures:
            try:
                await fn(client)
            except Exception as e:
                # One flaky service shouldn't block refreshing the others.
                print(f"{name}: FAILED ({type(e).__name__}: {e})")
                failed.append((name, f"{type(e).__name__}: {e}"))
    print(f"\nFixtures saved to {FIX_DIR}")
    if failed:
        print("\n⚠ Some captures failed — existing fixtures preserved:")
        for name, err in failed:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    asyncio.run(main())
