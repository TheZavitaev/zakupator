"""Auchan adapter.

Uses the public `/v3/autohints/` search suggestions endpoint, which returns
ready-to-use JSON with prices. No auth, no signing, no cookies required.
Verified live during recon — see docs/recon.md section "Ашан".

The endpoint only returns top N products (up to ~10), which is fine for our
"find this one product" use case. For full catalogue browsing a different
`/v3/` endpoint exists but we don't need it for MVP.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from zakupator.adapters.base import ServiceAdapter
from zakupator.models import Address, Offer, SearchResult, Service
from zakupator.net import FetchError, fetch_with_retry

# The frontend uses merchantId=3 as the umbrella e-commerce merchant — not a
# specific physical store. Store-level merchant ids live under /v3/shops/, but
# autohints works against the aggregate merchant.
_DEFAULT_MERCHANT_ID = 3

# Literal "W" — the frontend's channel for web. Don't ask, we verified it live.
_CHANNEL_WEB = "W"

_SEARCH_URL = "https://www.auchan.ru/v3/autohints/"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class AuchanAdapter(ServiceAdapter):
    service = Service.AUCHAN

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # If the caller didn't provide a shared client, we own one.
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={
                "accept": "application/json",
                "accept-language": "ru,en;q=0.9",
                "user-agent": _DEFAULT_USER_AGENT,
            },
        )

    async def search(self, query: str, address: Address, limit: int = 5) -> SearchResult:
        params = {
            "merchantId": _DEFAULT_MERCHANT_ID,
            "query": query,
            "productsSize": limit,
            "channel": _CHANNEL_WEB,
        }
        try:
            resp = await fetch_with_retry(
                self._client, "GET", _SEARCH_URL, params=params
            )
        except FetchError as e:
            return SearchResult(query=query, service=self.service, error=e.tag)

        if resp.status_code != 200:
            # Non-retryable non-200 (most 4xx). Bubble up the code.
            return SearchResult(
                query=query,
                service=self.service,
                error=f"http {resp.status_code}",
            )

        try:
            payload = resp.json()
        except ValueError:
            return SearchResult(query=query, service=self.service, error="non-json response")

        raw_products = (payload.get("data") or {}).get("products") or []
        offers = [self._offer_from_raw(p) for p in raw_products]
        # Drop anything that failed to parse (belt-and-braces).
        offers = [o for o in offers if o is not None]
        return SearchResult(query=query, service=self.service, offers=offers)

    @staticmethod
    def _offer_from_raw(raw: dict) -> Offer | None:
        # Minimum fields we need to be useful. If any of these is missing,
        # the product isn't worth showing.
        name = raw.get("name")
        price_str = raw.get("price")
        if not name or price_str is None:
            return None

        try:
            price = Decimal(str(price_str))
        except (ValueError, ArithmeticError):
            return None

        old_price_str = raw.get("oldPrice")
        price_original: Decimal | None = None
        if old_price_str is not None:
            try:
                price_original = Decimal(str(old_price_str))
            except (ValueError, ArithmeticError):
                price_original = None

        link_url = raw.get("link_url") or ""
        deep_link = f"https://www.auchan.ru{link_url}" if link_url.startswith("/") else link_url

        return Offer(
            service=Service.AUCHAN,
            product_id=str(raw.get("id", "")),
            title=name,
            price=price,
            price_original=price_original,
            in_stock=bool(raw.get("available", True)),
            image_url=raw.get("image_url"),
            deep_link=deep_link or None,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
