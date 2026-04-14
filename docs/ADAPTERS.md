# Adding a new service adapter

This is the checklist for wiring up a fourth (or fifth, ...) delivery
service. Follow it top-to-bottom; each step is small.

Before you start, **read [SPEC §2](SPEC.md#2-adapter-contract)** — the
guarantees an adapter MUST provide are non-negotiable. The biggest
footgun is raising exceptions; don't.

---

## 1. Reconnaissance

Before writing code, confirm the service is reachable from a vanilla
`httpx.AsyncClient`:

1. Open the service's web catalog search in Chrome DevTools → Network
   tab.
2. Type a query. Identify the request that returns the product data —
   usually JSON or GraphQL, occasionally HTML.
3. Right-click → Copy → Copy as cURL.
4. Paste into a terminal. Does it return the same data without the
   cookies / Authorization headers? If yes, proceed.
5. If auth / anti-bot / client-side signing is in the way, the service
   is out of scope for this architecture. Add a note to
   [docs/recon.md](recon.md) and stop.

Capture a real response for tests while you're here:

```bash
curl '<endpoint>' -H 'User-Agent: ...' -o tests/fixtures/<service>_search.json
```

---

## 2. Register the service

Add a new variant to the `Service` enum in `src/zakupator/models.py`:

```python
class Service(StrEnum):
    VKUSVILL = "vkusvill"
    AUCHAN = "auchan"
    METRO = "metro"
    NEWSERVICE = "newservice"  # <— new
```

The string value is a callback-data component and a persisted cart
column — once shipped, don't rename.

---

## 3. Write the adapter

Create `src/zakupator/adapters/<service>.py`. Subclass
`ServiceAdapter`. Minimum skeleton:

```python
from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from zakupator.adapters.base import ServiceAdapter
from zakupator.models import Address, Offer, SearchResult, Service
from zakupator.net import FetchError, fetch_with_retry

_ENDPOINT = "https://api.newservice.example/search"
_USER_AGENT = "Mozilla/5.0 (...) Chrome/147.0.0.0 Safari/537.36"


class NewServiceAdapter(ServiceAdapter):
    service = Service.NEWSERVICE

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def search(
        self, query: str, address: Address, limit: int = 5
    ) -> SearchResult:
        try:
            resp = await fetch_with_retry(
                self._client, "GET", _ENDPOINT,
                params={"q": query, "limit": limit},
            )
        except FetchError as e:
            return SearchResult(query=query, service=self.service, error=e.tag)

        if resp.status_code != 200:
            return SearchResult(
                query=query, service=self.service,
                error=f"http {resp.status_code}",
            )

        try:
            payload = resp.json()
        except ValueError:
            return SearchResult(
                query=query, service=self.service,
                error="non-json response",
            )

        offers: list[Offer] = [
            o for o in (self._offer_from_raw(p) for p in payload.get("items", []))
            if o is not None
        ]
        return SearchResult(query=query, service=self.service, offers=offers)

    @staticmethod
    def _offer_from_raw(raw: dict[str, Any]) -> Offer | None:
        name = raw.get("name")
        price_raw = raw.get("price")
        if not name or price_raw is None:
            return None
        try:
            price = Decimal(str(price_raw))
        except (ValueError, ArithmeticError):
            return None
        # ... extract product_id, deep_link, image_url, etc.
        return Offer(
            service=Service.NEWSERVICE,
            product_id=str(raw["id"]),
            title=name,
            price=price,
            deep_link=f"https://newservice.example{raw.get('url', '')}",
        )

    async def close(self) -> None:
        await self._client.aclose()
```

**Things to get right:**

- Never raise outside `asyncio.CancelledError`. Map every foreseeable
  failure to a `SearchResult(error=...)`.
- Prices are always `Decimal`, always constructed via `Decimal(str(x))`.
- `deep_link` and `image_url` are absolute URLs.
- `product_id` is a stable string that round-trips back to the same
  product.

---

## 4. Register in the engine

Edit `src/zakupator/search.py::build_default_adapters`:

```python
def build_default_adapters() -> list[ServiceAdapter]:
    return [
        VkusVillAdapter(),
        AuchanAdapter(),
        MetroAdapter(),
        NewServiceAdapter(),  # <— new
    ]
```

The list order determines result display order in `/search` and
reference priority in `/compare`. Put the new service where it
belongs.

---

## 5. Register labels in the bot

Edit `src/zakupator/bot.py`:

- `_SERVICE_LABELS` — human display name, e.g. `"🛒 НовыйСервис"`
- `_SERVICE_EMOJI` — single emoji for compact lines
- `_SERVICE_HOME` — homepage URL (used in cart group headers)
- `_SERVICE_CART_LINKS` — URL of the service's own cart page

The bot will not crash if you miss a dict, but labels will fall back
to the raw enum value, which is ugly.

---

## 6. Write tests

Adapter tests follow the pattern in
`tests/test_adapter_auchan.py`:

1. Put a captured raw response in `tests/fixtures/<service>_search.json`
   (or `.html`).
2. Build a `mock_client` (from `conftest.py`) that returns that
   fixture when the adapter hits its endpoint.
3. Assert: count of parsed offers, price is `Decimal`, deep_link is
   absolute, `product_id` is set.
4. Add at least one negative test: empty response, bad JSON, 503
   status → confirm the correct `error` tag.

Minimum expected coverage:

- happy path (fixture → N parsed offers)
- HTTP error → correct `error` tag
- transport error → `network` tag
- malformed response → `non-json response` tag

---

## 7. Update `scripts/capture_fixtures.py`

If the adapter reads a fixture that was captured live, add a clause
to `capture_fixtures.py` that re-captures it. The script is expected
to be resilient: per-service failures are logged, not fatal.

---

## 8. Run the gate

```bash
scripts/check.sh all
```

All of `ruff`, `mypy --strict`, `bandit`, `semgrep`, `pip-audit`, and
`pytest` must be green. CI will run the same gate.

---

## 9. Document in the spec

Add a bullet to [SPEC §1.1](SPEC.md#11-service-strenum) listing the
new `Service.*` value. If you introduced any new `error` tags, add
them to [SPEC §1.4](SPEC.md#14-searchresult-dataclass).

---

## 10. Update CHANGELOG

New service = minor version bump in `pyproject.toml::version` and a
line in `CHANGELOG.md` under `## [Unreleased]`.
