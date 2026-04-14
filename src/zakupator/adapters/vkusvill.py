"""VkusVill adapter.

VkusVill is server-side rendered (Bitrix). There's no product API to talk to —
instead we scrape the `/search/` page HTML. No auth, no captcha, stable
selectors bound to analytics hooks. Verified live, see docs/recon.md section
"ВкусВилл".

Key stability insight: the price nodes carry the class
`js-datalayer-catalog-list-price` (and `-old` for the pre-discount value).
These are hooks consumed by Yandex Metrica / GTM — breaking them would break
the company's marketing analytics. Of all scraping selectors this is about as
stable as it gets without an official API.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import httpx
from selectolax.parser import HTMLParser, Node

from zakupator.adapters.base import ServiceAdapter
from zakupator.models import Address, Offer, SearchResult, Service
from zakupator.net import FetchError, fetch_with_retry

_SEARCH_URL = "https://vkusvill.ru/search/"
_BASE_URL = "https://vkusvill.ru"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class VkusVillAdapter(ServiceAdapter):
    service = Service.VKUSVILL

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={
                "accept": "text/html,application/xhtml+xml",
                "accept-language": "ru,en;q=0.9",
                "user-agent": _DEFAULT_USER_AGENT,
            },
            follow_redirects=True,
        )

    async def search(self, query: str, address: Address, limit: int = 5) -> SearchResult:
        params = {"type": "products", "q": query}
        try:
            resp = await fetch_with_retry(self._client, "GET", _SEARCH_URL, params=params)
        except FetchError as e:
            return SearchResult(query=query, service=self.service, error=e.tag)

        if resp.status_code != 200:
            return SearchResult(
                query=query,
                service=self.service,
                error=f"http {resp.status_code}",
            )

        offers = self._parse_html(resp.text, limit=limit)
        return SearchResult(query=query, service=self.service, offers=offers)

    def _parse_html(self, html: str, *, limit: int) -> list[Offer]:
        tree = HTMLParser(html)

        # A product card is the smallest ancestor of a `/goods/` link that
        # also contains a price node. In the real markup, `.ProductCard__link`
        # is the anchor with the product name and href, so we anchor on that.
        card_links = tree.css("a.ProductCard__link[href*='/goods/']")
        if not card_links:
            # Fallback selector — class may have extra modifiers.
            card_links = tree.css("a[class*='ProductCard__link'][href*='/goods/']")

        offers: list[Offer] = []
        seen_ids: set[str] = set()
        for link in card_links:
            # Walk up until we hit the price hook's owning container.
            container = self._find_card_container(link)
            if container is None:
                continue

            offer = self._offer_from_card(container, link)
            if offer is None:
                continue
            if offer.product_id and offer.product_id in seen_ids:
                continue
            if offer.product_id:
                seen_ids.add(offer.product_id)
            offers.append(offer)
            if len(offers) >= limit:
                break
        return offers

    @staticmethod
    def _find_card_container(link: Node) -> Node | None:
        node: Node | None = link
        for _ in range(8):
            if node is None:
                return None
            if node.css_first(".js-datalayer-catalog-list-price") is not None:
                return node
            node = node.parent
        return None

    def _offer_from_card(self, card: Node, link: Node) -> Offer | None:
        # Price. `js-datalayer-catalog-list-price` holds the integer kopeck-free
        # price as inner text. We take the first match inside this specific
        # card subtree to avoid picking a neighbour card's price.
        price_node = card.css_first(".js-datalayer-catalog-list-price")
        if price_node is None:
            return None
        price = self._parse_price(price_node.text(strip=True))
        if price is None:
            return None

        price_original: Decimal | None = None
        old_node = card.css_first(".js-datalayer-catalog-list-price-old")
        if old_node is not None:
            price_original = self._parse_price(old_node.text(strip=True))
            # Old node may exist but be empty on non-discounted items.
            if price_original is not None and price_original <= 0:
                price_original = None

        # Product name: the anchor's inner text (whitespace-normalized).
        title = (link.text(strip=True) or "").strip()
        if not title:
            title = self._read_attr(link, "title") or self._read_attr(link, "aria-label") or ""
        if not title:
            return None

        href = self._read_attr(link, "href") or ""
        deep_link = f"{_BASE_URL}{href}" if href.startswith("/") else (href or None)

        # Product id from the href: /goods/<slug>-<digits>.html
        product_id = self._product_id_from_href(href)

        # Weight (free-form: "930 мл", "200 г", "шт" etc)
        weight_node = card.css_first(".ProductCard__weight")
        amount_unit = (weight_node.text(strip=True) if weight_node else "") or None

        # Image. VkusVill lazy-loads many images, so look at data-src first.
        image_url = None
        img = card.css_first("img")
        if img is not None:
            image_url = (
                self._read_attr(img, "data-src")
                or self._read_attr(img, "data-lazy")
                or self._read_attr(img, "src")
            )
            if image_url and image_url.startswith("/"):
                image_url = f"{_BASE_URL}{image_url}"

        return Offer(
            service=Service.VKUSVILL,
            product_id=product_id or title[:40],
            title=title,
            price=price,
            price_original=price_original,
            amount_unit=amount_unit,
            in_stock=True,
            image_url=image_url,
            deep_link=deep_link,
        )

    @staticmethod
    def _parse_price(text: str) -> Decimal | None:
        if not text:
            return None
        # VkusVill prices are integer rubles in this hook. Normalize commas
        # just in case they ever add decimals.
        cleaned = text.replace("\xa0", "").replace(" ", "").replace(",", ".")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _read_attr(node: Node, name: str) -> str | None:
        attrs = node.attributes or {}
        value = attrs.get(name)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _product_id_from_href(href: str) -> str | None:
        # /goods/moloko-2-5-v-butylke-900-ml-36296.html → "36296"
        if not href:
            return None
        tail = href.rsplit("/", 1)[-1]
        tail = tail.removesuffix(".html")
        segments = tail.rsplit("-", 1)
        if len(segments) == 2 and segments[1].isdigit():
            return segments[1]
        return tail or None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
