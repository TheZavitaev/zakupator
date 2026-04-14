# Zakupator — Specification

Contract for everyone touching the code: what the shapes are, what
adapters promise, what the bot promises, and where the magic numbers
came from. README.md is the friendly overview; this file is the
authoritative reference.

Version corresponds to `pyproject.toml::version`. Currently **0.1.0**.

---

## 1. Data model

All types live in `src/zakupator/models.py`. They are deliberately small
and framework-free so adapters and the bot can share them without
import cycles.

### 1.1 `Service` (StrEnum)

```
Service.VKUSVILL = "vkusvill"
Service.AUCHAN   = "auchan"
Service.METRO    = "metro"
```

The string value is used as a callback-data component, a persisted cart
column, and a task name inside `SearchEngine`. Do not rename without a
migration.

### 1.2 `Address` (frozen dataclass)

| field  | type    | meaning                                      |
|--------|---------|----------------------------------------------|
| label  | str     | user-facing label ("Дом", "Офис")            |
| text   | str     | full address string                          |
| lat    | float   | WGS84 latitude, required                     |
| lon    | float   | WGS84 longitude, required                    |

Adapters receive an Address; they may ignore it (Metro currently picks
a hardcoded Moscow store), but the argument is mandatory so we can add
geo-gated adapters later without a signature change.

### 1.3 `Offer` (dataclass)

| field           | type              | required | meaning                              |
|-----------------|-------------------|----------|--------------------------------------|
| service         | Service           | yes      | which adapter produced this          |
| product_id      | str               | yes      | service-local id, stable enough to round-trip |
| title           | str               | yes      | display name                         |
| price           | Decimal           | yes      | final per-unit price the user pays   |
| price_original  | Decimal \| None   | no       | pre-discount, only if the service exposes it |
| unit            | str \| None       | no       | "шт", "кг", "л"                      |
| amount          | float \| None     | no       | package size value                   |
| amount_unit     | str \| None       | no       | "мл", "г", "шт"                      |
| in_stock        | bool              | no       | defaults to True                     |
| image_url       | str \| None       | no       | absolute URL                         |
| deep_link       | str \| None       | no       | product page URL on the service      |

Money is always `Decimal`. Never use `float` for prices — rapid accretion
of FP error shows up as "93.000000001" in the UI.

### 1.4 `SearchResult` (dataclass)

```
SearchResult(
    query: str,
    service: Service,
    offers: list[Offer] = [],
    error: str | None = None,
)
```

**Invariant**: exactly one of `offers` and `error` is meaningful. On
error, `offers` is the empty list; callers MAY render "service
unavailable" text keyed on `error`.

`error` is a short machine-readable tag. Known values:

| tag                | origin                       | meaning                          |
|--------------------|------------------------------|----------------------------------|
| `http N`           | non-2xx HTTP                 | retries exhausted or non-retryable |
| `network`          | httpx protocol failure       | non-retryable transport error    |
| `timeout`          | SearchEngine deadline        | adapter didn't finish within the per-batch timeout |
| `non-json response`| adapter JSON parse           | upstream returned HTML or garbage |
| `gql: <msg>`       | Metro                        | GraphQL errors[0].message, truncated |
| `unhandled: ...`   | SearchEngine._safe_search    | adapter raised an unexpected exception |

The bot's `_humanize_error` translates these into Russian user copy.
Add new tags in `net.py::FetchError.tag` or adapter code; update the
humanizer in `bot.py`.

---

## 2. Adapter contract

Every adapter subclasses `zakupator.adapters.base.ServiceAdapter`
(`src/zakupator/adapters/base.py`).

```python
class ServiceAdapter(ABC):
    service: Service  # class attribute

    @abstractmethod
    async def search(
        self, query: str, address: Address, limit: int = 5
    ) -> SearchResult: ...

    async def close(self) -> None: ...  # default no-op
```

### 2.1 Guarantees the adapter MUST provide

1. **Never raise on a foreseeable failure.** Transport errors, non-200
   responses, parse errors, empty results — all MUST be returned as a
   `SearchResult` with an `error` tag set. The only exception is
   `asyncio.CancelledError`, which MUST propagate (SearchEngine uses
   cancellation as its timeout mechanism).
2. **Honor `limit`.** Return at most `limit` offers. Services with
   their own pagination MUST pass this through.
3. **Decimal prices.** `Offer.price` is always a `Decimal`. Converting
   from strings via `Decimal(str(raw))` is the safe pattern.
4. **Absolute URLs.** `deep_link` and `image_url` MUST be absolute
   (`https://…`), not path-relative. Upstream APIs often return
   relative paths — prefix them during parsing.
5. **Stable `product_id`.** The id MUST round-trip back to the same
   item on the service, because the bot stores it in the cart and
   reuses it in callback data.
6. **Idempotent.** Calling `search()` twice with the same args MUST NOT
   produce user-visible side effects (beyond cache warming).

### 2.2 Guarantees the caller (SearchEngine) provides

1. A single `httpx.AsyncClient` per adapter instance, reused across
   calls. Adapters MUST close it in `close()`.
2. Wall-clock deadline enforcement via task cancellation. Adapters
   don't need to implement their own total-timeout; per-request timeouts
   on the httpx client are still sensible.
3. Retries on transient failures via `net.fetch_with_retry`. Adapters
   SHOULD use it instead of raw `client.request()`.

### 2.3 `net.fetch_with_retry`

`src/zakupator/net.py`.

```
fetch_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    **kwargs,
) -> httpx.Response
```

- Retries **transport errors** (`TimeoutException`, `ConnectError`,
  `ReadError`) unconditionally.
- Retries **HTTP status** in `{408, 429, 500, 502, 503, 504}`.
- Does NOT retry other `httpx.HTTPError` subclasses (protocol errors
  etc) — raises `FetchError("network", …)`.
- Does NOT retry non-retryable 4xx (404/403/etc) — returns the response
  as-is, caller decides what to do.
- `DEFAULT_POLICY = RetryPolicy(max_attempts=3, backoff=(0.3, 1.0))`.
- On final failure raises `FetchError` with `reason` + optional `status`.
  `.tag` property produces the stable short tag for `SearchResult.error`.

---

## 3. Cross-service matching

`src/zakupator/matching.py`.

### 3.1 Quantity extraction

`extract_quantity(name: str) -> Quantity | None`

Regex-based. Recognizes Russian unit abbreviations:

| unit token                        | base class | base unit |
|-----------------------------------|------------|-----------|
| `мл`, `миллилитр`                 | volume     | ml        |
| `л`, `литр`                       | volume     | ml (×1000)|
| `г`, `гр`, `грамм`                | mass       | g         |
| `кг`, `килограмм`                 | mass       | g (×1000) |
| `шт`, `штук`                      | pieces     | count     |

Picks the **first** quantity mention in the name — secondary mentions
("230 ккал на 100 г") are ignored.

### 3.2 Equality heuristic

`is_same_product(a: Offer, b: Offer) -> bool` — two offers from
different services are "the same product" iff:

1. `rapidfuzz.fuzz.token_set_ratio(a.title, b.title) ≥ 80`.
2. If both names carry a parseable quantity:
   - same `unit_class`, AND
   - `min/max ratio ≥ 1 − 0.12` (≤12% relative difference).
3. If either name has no parseable quantity, fall back to name alone.

**Thresholds are deliberately strict.** False positives (calling two
different products "the same") mislead users about the cheapest deal.
False negatives just show the `/compare` "нет совпадений" fallback,
which is honest.

Configured constants in the file:

```python
_NAME_SIM_THRESHOLD    = 80
_QTY_RELATIVE_TOLERANCE = 0.12
```

### 3.3 Cross-service search

`find_matches(reference, candidates)` returns at most one
`MatchedOffer` per *other* service, picking the highest-scoring
same-product candidate. Services where nothing matches are absent.

`cheapest_across_matches(reference, matches)` picks the min-price
`Offer` across `{reference} ∪ matches.offer`, returning both the winner
and the absolute savings vs the reference.

---

## 4. Response cache

`src/zakupator/response_cache.py`.

- Keyed by `(service.value, normalized_query, limit)`.
- Normalization: `query.casefold().strip()` + whitespace collapse.
- `max_size=256`, `ttl=300s` (5 min) by default.
- **Only caches successful non-empty results.** Errors and empty result
  sets are deliberately not cached — retry-on-click is cheap, and
  caching failures compounds user pain.
- LRU eviction on insert; lazy TTL check on get.
- Process-local; multi-worker deploys will need Redis.

---

## 5. Search cache (UI callback cache)

`src/zakupator/search_cache.py`.

Separate from the response cache. Purpose: when the user sees
`/search молоко` and clicks "в корзину" under item #2, we need the
exact `Offer` frozen at display time (price, deep_link etc). Re-running
the query risks a price race — the user saw 100 ₽, by click time it's
110 ₽.

- Keyed by a 6-char token from `string.ascii_lowercase + digits`
  (collision space ~2×10⁹, cryptographic randomness via `secrets.choice`).
- `max_size=512`, `ttl=1800s` (30 min).
- Callback data format: `a:<token>:<flat_index>` where `flat_index` is
  the position in the flattened `service × offer` list, ordered by
  `SearchEngine` result order.
- **Process-local.** Multi-worker deploys lose cart-add buttons on
  restart. The bot's error handling surfaces "Товар больше не найден"
  in that case.

---

## 6. Bot contract

`src/zakupator/bot.py`.

### 6.1 Commands

| Command                 | Implementation         | Notes                       |
|-------------------------|------------------------|-----------------------------|
| `/start`, `/help`       | `on_start`, `on_help`  | Help copy also catches unknown commands |
| `/search <q>`           | `on_search`            | `q` required                |
| plain text (not `/cmd`) | `on_plain_text`        | Equivalent to `/search`      |
| `/compare <q>`          | `on_compare`           | Requires ≥2 services to return offers |
| `/cart`                 | `on_cart`              | Inline keyboard with qty controls |
| `/total`                | `on_total`             | One line per service        |
| `/history`              | `on_history`           | Last 10 unique queries       |
| `/clear`                | `on_clear`             | Two-step confirmation        |

### 6.2 Callback data schema

Telegram limits callback_data to 64 bytes. Current schema (all ASCII):

| prefix      | shape               | meaning                           |
|-------------|---------------------|-----------------------------------|
| `a:TOK:N`   | token + flat index  | Add offer to cart from /search    |
| `r:ID`      | cart item id        | Remove cart item                  |
| `q:+:ID`    | cart item id        | Increment quantity                |
| `q:-:ID`    | cart item id        | Decrement quantity (may delete)   |
| `q:?:ID`    | cart item id        | Informational tap on middle label |
| `c:ask`     | —                   | Open clear-cart confirmation      |
| `c:yes`     | —                   | Confirm clear                     |
| `c:no`      | —                   | Cancel clear                      |
| `cp:list`   | —                   | Render plain-text cart for copy   |
| `h:Q`       | query string        | Repeat historic search            |

All handlers defensively `callback.answer()` on malformed data.

### 6.3 Error humanization

`_humanize_error(tag: str) -> str` maps the tags from §1.4 to Russian
user copy. Add new tags both in adapters/`net.py` AND in `_humanize_error`
— missing the humanizer falls through to a generic "временно недоступен".

---

## 7. Persistence

`src/zakupator/db.py`. SQLAlchemy 2.0 async, SQLite by default, Postgres
supported by swapping `DATABASE_URL`.

Tables:

- `users (id, telegram_id UNIQUE, username?, created_at)`
- `addresses (id, user_id FK, label, text, lat, lon, created_at)`
- `cart_items (id, user_id FK, service, service_product_id, title,
  price Numeric(10,2), quantity, deep_link?, added_at)`
- `search_history (id, user_id FK indexed, query, searched_at indexed)`

`cart_repo.py` is the only module that writes to these tables. Handlers
MUST NOT construct ORM objects directly.

---

## 8. Configuration

`src/zakupator/config.py` + pydantic-settings reads from environment
or `.env`. Public settings:

| env var                 | default                               | meaning                         |
|-------------------------|---------------------------------------|---------------------------------|
| `TELEGRAM_BOT_TOKEN`    | —                                     | required                        |
| `DATABASE_URL`          | `sqlite+aiosqlite:///./zakupator.db`  | SQLAlchemy URL                  |
| `LOG_LEVEL`             | `INFO`                                | Python logging level            |
| `DEFAULT_ADDRESS_*`     | Red Square, Moscow                    | Fallback geo until /address UX  |

---

## 9. Testing contract

- `pytest -q` runs the offline suite. **Zero network calls** in the
  default run; any test that would hit the wire MUST be marked
  `@pytest.mark.live` and is excluded by `addopts = "-m 'not live'"`.
- `tests/conftest.py::mock_client` is the canonical way to fake httpx
  responses — it builds a `MockTransport`-backed `AsyncClient`.
- Adapter tests live next to captured fixtures in `tests/fixtures/`.
  Refresh fixtures with `scripts/capture_fixtures.py` (requires live
  network).

---

## 10. Versioning

Semver-ish. The public contracts for this project are:

- the Telegram command grammar (§6.1)
- the callback-data schema (§6.2)
- `DATABASE_URL` schema

Breaking changes to any of these require a minor bump and a `CHANGELOG.md`
entry with a migration note.
