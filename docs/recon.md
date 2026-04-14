# Recon: grocery delivery services, RU market

Methodology per service:
1. Navigate to landing page in Chrome (via claude-in-chrome extension)
2. Install fetch/XHR interceptor BEFORE any user interaction
3. Trigger a product search with an uncommon query (avoid cache)
4. Analyze captured request: URL, method, required headers, body shape, response shape
5. Test which headers are required by replaying with fewer headers
6. Classify service by difficulty tier

## Difficulty tiers

- **Tier 0 — Open:** no auth needed, `httpx` hits it directly.
- **Tier 1 — Short-lived token:** auth token refreshable via a no-auth endpoint, no request signing.
- **Tier 2 — Request signing:** every request needs a client-computed signature (HMAC / custom). Must be reversed from JS bundle OR driven via Playwright.
- **Tier 3 — Full lockdown:** Cloudflare/SmartCaptcha gate, device fingerprint, cannot bypass from Python at all.

---

## 1. Самокат (samokat.ru) — **Tier 2**

- Search endpoint: `POST https://api-web.samokat.ru/search/products`
- Body: `{"query": "...", "showcaseIds": ["<dark-store-id>"]}`
- Required headers:
  - `authorization: Bearer <JWT, 652 chars>` — RS256, aud/sub/device_id/scope/user/jti/client_id. **TTL = 300s** (5 min).
  - `x-application-platform: web`
  - `deviceid: <21 char opaque>`
  - `x-creeper: <284 char base64>` — **per-request client signature**, blocker.
  - `content-type: application/json`
- Verified: replaying full headers unchanged returns `401 signature is invalid`. So creeper is either single-use (nonce) or incorporates a body/timestamp hash.
- Also uses `x-excreeper` header on telemetry endpoints (colibri-api), same family.
- JWT payload structure (decoded header only): `{alg:RS256, kid, typ:JWT}`. Claims: `aud, sub, device_id, scope, exp, iat, user, jti, client_id`.
- **Verdict:** cannot be called from Python directly. Options: (a) reverse-engineer `x-creeper` from their bundle, (b) Playwright, (c) skip.

---

## 2. Яндекс Лавка (lavka.yandex.ru) — **Tier 3 (conditional)**

- First contact: navigating directly to `lavka.yandex.ru` with an automated Chrome context **immediately** redirects to Yandex SmartCaptcha (`/showcaptcha?...`). The landing HTML is never served.
- Message: "Запросы с вашего устройства похожи на автоматические" — bot detection triggered before any interaction.
- This is a reputation/fingerprint gate: a clean residential browser passes it most of the time; automation-flagged contexts see it regardless of behavior.
- **Implication for us:** even a Playwright-driven adapter will periodically (or immediately) hit SmartCaptcha and need a human to solve it. This is a fundamental barrier for Lavka specifically, not something we can engineer around.
- API structure was not observable from this session (captcha blocked it).
- **Verdict:** out of reach for autonomous scraping. Would require a residential proxy + real browser + human-in-the-loop for captchas. Tentatively drop from scope.


## 3. Ozon Fresh (ozon.ru/category/fresh-9200) — **Tier 3**

- Navigating to the Fresh category page immediately serves Ozon's own puzzle captcha ("Сопоставьте пазл, двигая ползунок"), not the product listing.
- Ozon maintains its own in-house antibot (branded "Antibot Captcha" in the tab title). Known to be aggressive — they fingerprint TLS, canvas, WebGL, and behavioral signals.
- API structure was not observable.
- **Verdict:** same class as Lavka. Out of reach without residential proxies + stealth Playwright + captcha solving. Drop from scope for this project.


## 4. Купер (kuper.ru, ex-СберМаркет) — **Tier 2 (behavioral)**

- Landing page loads without any challenge — the HTML is served normally.
- First observation: Kuper is an **aggregator** — Metro, Лента, Ашан, ВкусВилл and others are sold through it, so in principle one adapter here would unlock several retailers at once.
- Triggered search via Enter key on the input → navigation to `/multisearch?q=...` → immediately redirected to `/xpvnsulc/?hcheck=...&request_ip=<real-IP>&request_id=...` challenge page (mint-green "Разверните картинку горизонтально" — Variti antibot).
- Challenge is **behavioral**, not IP/fingerprint — the real user IP (Russian residential) was passed through. Reason we hit it: bot-like interaction pattern (keyboard-only input, no mouse movement, Enter immediately). A human-paced session should pass.
- Did not get to observe search-endpoint structure because of the challenge redirect.
- **Verdict:** potentially reachable via Playwright with stealth + human-like timing. Probably not reachable from raw `httpx`. Needs a more patient second pass to characterize fully.
- **Note on aggregator play:** because Kuper fronts multiple retailers, a working Kuper adapter would be disproportionately valuable — it's "one integration, many catalogs". This makes it worth paying the Playwright tax for specifically.


## 5. ВкусВилл (vkusvill.ru) — **Tier 0 — HERO**

- Landing and search both load cleanly, no captcha, no authentication, no address gate.
- Search URL (direct GET, hittable from `httpx`): `https://vkusvill.ru/search/?type=products&q=<query>`
- Response: **server-side rendered HTML** (Bitrix-based). No client-side API calls for product data — the search results page already contains 50 product cards in the initial HTML.
- Custom ruble icon font (`currency-icons.woff2`) means prices don't contain the literal ₽ symbol in HTML; the number is separate from the currency glyph. Not a problem for parsing — just read the number node.
- **Stable selectors identified (from live DOM inspection):**
  - Card container: `.ProductCard__content`
  - Product link (to details): `a.ProductCard__link`, `href="/goods/<slug>/"`
  - Weight/amount: `.ProductCard__weight`
  - Price wrapper: `.ProductCard__price`
  - Price value: `.js-datalayer-catalog-list-price` — **analytics hook**, stable by contract (breaking it would break their Yandex Metrica events)
  - Old price (when discounted): `.js-datalayer-catalog-list-price-old`
  - Currency glyph: `.CurrencyIcon`
- Analytics-bound classes are a strong signal the selectors won't churn.
- **Verdict:** straightforward scrape. `httpx.get` + `selectolax` parser. No JS execution needed. Address handling comes later (needed for accurate stock/price by region, but city-level default works for MVP).
- **This is the service we start with.**


## 6. Перекрёсток Впрок (vprok.ru) — **Tier 3**

- Root and /catalog/ both return an `Ошибка #625116` error page (Qrator-family block).
- The block fires before any markup renders. Automated Chrome is simply not allowed through.
- **Verdict:** drop from scope.

## 7. Магнит Доставка (magnit.ru/catalog) — **Tier 3**

- Returns "Выключите VPN, и всё заработает" message — Magnit's IP reputation check flagged the session even on the user's real residential Russian IP.
- Aggressive IP/ASN filtering. Regular Playwright won't reliably pass.
- **Verdict:** drop from scope.

## 8. Лента Онлайн (lenta.com) — **Tier 1 (deferred)**

- Landing loads fine, no captcha.
- Clean REST API: `/api-gateway/v1/...` (classic gateway pattern).
- Product search endpoint: `POST /api/rest/productSearch` — responds with `{Head, Body}` envelope (`Head` has `RequestId, Created, Method, ServerIp, Status, UserSegment, ServerName`).
- Error returned when calling without auth: `Utk\Utkapi_Exception_MarketingPartner_InvalidKey — "Неверный ключ партнера по продажам"`. A constant marketing-partner key is required, embedded somewhere in the JS bundle (not found in main.js — lives in a lazy chunk).
- Also uses **Kaspersky bot detection** (`sitecdn.api.lenta.com/assets-ng/scripts/kaspersky/das.obf.js`). May be lenient now, strict later.
- **Verdict:** solvable with finite effort (extract marketingPartnerKey from the bundle, deal with Kaspersky if it bites). Not trivial, but not an impossible blocker like Samokat's per-request signing. **Deferred** — pick up after the Tier 0 services are in place.

## 9. Ашан (auchan.ru) — **Tier 0 — HERO**

- Landing and search both load cleanly.
- Uses a clean REST API at `/v3/...`. No authentication required.
- Store directory: `GET /v3/shops/?regionId=<id>` → returns `{shops: [...], services: [...]}`. Each shop has: `merchant_id, name, name_auchan, region_id, address_string, geo_lat, geo_lon, delivery, express_delivery, pickup, delivery_methods, format, phone, ...`. Moscow region alone has 60 hypermarkets.
- Product search endpoint: `GET /v3/autohints/?merchantId=3&query=<q>&productsSize=<n>&channel=W`
  - Verified live: returns `{data: {query, correction, sts, taps, categories, products}}` with 10 products on a simple query.
  - `channel=W` (literal "W" = Web) is the magic param the frontend uses.
  - `merchantId=3` is the umbrella e-commerce merchant (not a specific store). The shops endpoint returns store-level merchant_ids too, but autohints works with the aggregate one.
- **Product schema** (verified on a live call):
  ```
  {
    id: int,
    available: bool,
    name: str,
    price: str,           // e.g. "97.99"
    oldPrice: str | null, // presence indicates discount
    isAdult: bool,
    score: float,         // ranking
    link_url: str,        // relative URL
    image_url: str
  }
  ```
- **Verdict:** one of the cleanest APIs in the whole RU grocery landscape. Direct `httpx.get(...)`, no browser needed, returns ready-to-use JSON with prices, names, images, links.
- Caveat: `autohints` returns top N suggestions (10 is enough for a bot's "compare this one product" flow). For full catalogue / category browse a different endpoint under `/v3/` likely exists — can be characterized later when needed.

## 10. Metro (online.metro-cc.ru) — **Tier 0 — HERO**

- Landing and search load cleanly. Default address gets assigned automatically.
- Backed by **GraphQL** at `https://supergraph.metro-cc.ru/graphql`.
- **Introspection is enabled in production** — the full schema is queryable.
- Root query fields available: `search, category, categoryTree, product_placements, banner_placements, promoBanners, sliderProducts, getReviews, getArticles, ...` (25 total).
- **Search signature (verified live):**
  ```graphql
  {
    search(text: "молоко") {
      products(storeId: 10, size: 5) {
        total
        products { id article ... }
      }
      suggestions
      categories { ... }
    }
  }
  ```
  Returned `total: 263` products for "молоко", 5 items per the `size` param.
- `Product` type has rich fields: `id, article, contentId, name (via stock), barcodes, category, description, attributes, ...` (50+ fields).
- `storeId` is required on `products` and identifies the physical Metro warehouse. `10` worked from the default Moscow session. A store list can be fetched via a separate query (not yet explored).
- No authentication required for `search` — `{__typename}` ping returns 200, introspection returns 200, data queries return 200.
- **Verdict:** best-in-class. GraphQL means we can request exactly the fields we need and nothing more — small responses, no HTML parsing, no selector churn. Rich enough data to also support product detail pages later.

---

# Summary & Strategic Picture

| Service | Tier | Note |
|---|---|---|
| ВкусВилл | **0** | Scrape HTML, zero auth, stable selectors |
| Ашан | **0** | REST `/v3/autohints/?merchantId=3&channel=W` |
| Metro | **0** | GraphQL + introspection enabled |
| Лента | 1 | REST works, needs marketingPartnerKey from JS bundle |
| Купер | 2 behavioral | Aggregator — valuable but gated by Variti on search |
| Самокат | 2 | `x-creeper` per-request signature — reverse JS or Playwright |
| Яндекс Лавка | 3 | SmartCaptcha at the edge |
| Ozon Fresh | 3 | Puzzle captcha at the edge |
| Перекрёсток Впрок | 3 | Qrator edge block |
| Магнит | 3 | IP reputation block |

**Three hero services are free and clean.** That's a real bot, not a toy. The user's originally-requested four (Озон Фреш, Лавка, Самокат, Купер) turned out to be the four hardest services on the market — a consequence of them being the most popular and thus most aggressively protected.

User's real IP seen during recon: `91.149.255.80` (Moscow residential). IP reputation was not an issue on any Tier 0 service.

