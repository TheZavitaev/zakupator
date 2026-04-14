"""Metro adapter.

Talks to Metro's GraphQL supergraph directly. No auth required — verified live
during recon, see docs/recon.md section "Metro".

Schema reachable via introspection at https://supergraph.metro-cc.ru/graphql .
All field names / types below have been verified against the live schema.

We hardcode a `storeId` because `search.products(storeId: Int!)` is mandatory.
Metro has physical warehouses keyed by id; 10 is the Moscow store that the
default web session picks. For multi-region support later we'll need a
store-lookup call or an address→store resolver.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from zakupator.adapters.base import ServiceAdapter
from zakupator.models import Address, Offer, SearchResult, Service
from zakupator.net import FetchError, fetch_with_retry

_ENDPOINT = "https://supergraph.metro-cc.ru/graphql"
_DEFAULT_STORE_ID = 10  # Moscow, picked up automatically by the web session

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# Verified live against the real schema. `stocks` returns per-store stock info,
# already scoped to the storeId of the outer `products(storeId: ...)` call —
# we typically get exactly one Stock entry back. Note the snake_case field
# names inside Price — Metro's schema is snake_case internally despite the
# camelCase convention on the root `search` field.
_SEARCH_QUERY = """
query Search($text: String!, $storeId: Int!, $size: Int!) {
  search(text: $text) {
    products(storeId: $storeId, size: $size) {
      total
      products {
        id
        article
        name
        url
        slug
        images
        manufacturer { name }
        stocks {
          store_id
          eshop_availability
          value
          text
          prices {
            price
            old_price
            discount
            is_promo
          }
        }
      }
    }
  }
}
""".strip()


class MetroAdapter(ServiceAdapter):
    service = Service.METRO

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        store_id: int = _DEFAULT_STORE_ID,
    ) -> None:
        self._store_id = store_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "accept-language": "ru,en;q=0.9",
                "user-agent": _DEFAULT_USER_AGENT,
                "origin": "https://online.metro-cc.ru",
                "referer": "https://online.metro-cc.ru/",
            },
        )

    async def search(self, query: str, address: Address, limit: int = 5) -> SearchResult:
        payload = {
            "query": _SEARCH_QUERY,
            "variables": {
                "text": query,
                "storeId": self._store_id,
                "size": limit,
            },
        }
        try:
            resp = await fetch_with_retry(
                self._client, "POST", _ENDPOINT, json=payload
            )
        except FetchError as e:
            return SearchResult(query=query, service=self.service, error=e.tag)

        if resp.status_code != 200:
            return SearchResult(
                query=query,
                service=self.service,
                error=f"http {resp.status_code}",
            )

        try:
            body = resp.json()
        except ValueError:
            return SearchResult(query=query, service=self.service, error="non-json response")

        if body.get("errors"):
            msg = str(body["errors"][0].get("message", ""))[:120]
            return SearchResult(query=query, service=self.service, error=f"gql: {msg}")

        products = (
            ((body.get("data") or {}).get("search") or {}).get("products") or {}
        ).get("products") or []
        offers = [self._offer_from_raw(p) for p in products]
        offers = [o for o in offers if o is not None]
        return SearchResult(query=query, service=self.service, offers=offers)

    def _offer_from_raw(self, raw: dict) -> Offer | None:
        name = raw.get("name")
        if not name:
            return None

        # Pick the stock entry for our store (typically just one since we
        # scoped the outer query). Fall back to the first available if the
        # store_id filter doesn't match for some reason.
        stocks = raw.get("stocks") or []
        stock = next(
            (s for s in stocks if s and s.get("store_id") == self._store_id),
            stocks[0] if stocks else None,
        )
        if not stock:
            return None

        prices = stock.get("prices") or {}
        price_raw = prices.get("price")
        if price_raw is None:
            return None
        try:
            price = Decimal(str(price_raw))
        except (ValueError, ArithmeticError):
            return None

        old_raw = prices.get("old_price")
        price_original: Decimal | None = None
        if old_raw is not None:
            try:
                price_original = Decimal(str(old_raw))
            except (ValueError, ArithmeticError):
                price_original = None

        url = raw.get("url") or (f"/products/{raw.get('slug')}" if raw.get("slug") else "")
        deep_link = (
            f"https://online.metro-cc.ru{url}" if url.startswith("/") else (url or None)
        )

        images = raw.get("images") or []
        image_url = images[0] if images else None

        # Note: Metro's Stock has `value` + `text` but `text` is free-form
        # inventory status ("Заканчивается", "В наличии" etc), not a unit.
        # The real package size is already embedded in the product name
        # ("...970мл"), so we leave amount/amount_unit unset here.

        return Offer(
            service=Service.METRO,
            product_id=str(raw.get("id", raw.get("article", ""))),
            title=name,
            price=price,
            price_original=price_original,
            in_stock=bool(stock.get("eshop_availability", True)),
            image_url=image_url,
            deep_link=deep_link,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
