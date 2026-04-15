"""Pure presentation helpers for the Telegram bot.

Everything here is side-effect-free: formatting product offers into HTML
strings, building inline keyboards, picking cross-service matches,
translating short error tags into user-facing Russian. The handlers
module imports from here; nothing in here imports from handlers.

Keeping presentation separate means this whole file is trivially unit
testable without spinning up an aiogram Bot or Dispatcher, which is
exactly how `tests/test_bot_formatters.py` exercises it.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.markdown import hbold, hlink

from zakupator import cart_repo
from zakupator.callbacks import AddToCart, ChangeQty, ClearCart, CopyCart, RemoveItem
from zakupator.constants import CART_TITLE_TRUNCATE
from zakupator.matching import MatchedOffer, find_matches
from zakupator.models import Offer, SearchResult, Service

# Pretty service labels for UI. Keep them short.
_SERVICE_LABELS: dict[Service, str] = {
    Service.VKUSVILL: "🥬 ВкусВилл",
    Service.AUCHAN: "🛒 Ашан",
    Service.METRO: "📦 Metro",
}

_SERVICE_EMOJI: dict[Service, str] = {
    Service.VKUSVILL: "🥬",
    Service.AUCHAN: "🛒",
    Service.METRO: "📦",
}

# Direct links to each service's main entry point — used in /cart headers.
_SERVICE_HOME: dict[Service, str] = {
    Service.VKUSVILL: "https://vkusvill.ru/",
    Service.AUCHAN: "https://www.auchan.ru/",
    Service.METRO: "https://online.metro-cc.ru/",
}

# Deep links to each service's own cart page, for the "open in service"
# button under `/cart`. Each service routes an anonymous user to login if
# needed, then drops them on their cart.
_SERVICE_CART_LINKS: dict[Service, str] = {
    Service.VKUSVILL: "https://vkusvill.ru/cart/",
    Service.AUCHAN: "https://www.auchan.ru/cart/",
    Service.METRO: "https://online.metro-cc.ru/cart",
}


# ---- error humanization --------------------------------------------------


def _humanize_error(raw: str) -> str:
    """Convert an adapter's short error tag into Russian user-friendly text.

    Adapters emit short, machine-readable codes like "network", "timeout",
    "http 503". The bot translates them here so /search and /compare
    messages don't look like crash logs.
    """
    if not raw:
        return "неизвестная ошибка"
    raw_lc = raw.lower()
    if raw_lc == "timeout":
        return "не ответил вовремя"
    if raw_lc == "network":
        return "временно недоступен"
    if raw_lc.startswith(("http 5", "http 429")):
        return "временно недоступен"
    if raw_lc.startswith("http 4"):
        return "отклонил запрос"
    if raw_lc.startswith("gql:"):
        return "ответил ошибкой"
    if raw_lc.startswith("non-json"):
        return "отдал некорректный ответ"
    if raw_lc.startswith("unhandled:"):
        return "упал с неизвестной ошибкой"
    # Fallback: show the raw code but keep it compact.
    return raw[:60]


# ---- low-level string helpers --------------------------------------------


def _format_price(price: Decimal) -> str:
    # 94.99 → "94.99", 97.00 → "97"
    quantized = price.normalize()
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


_ESCAPE_MAP = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def _escape(text: str) -> str:
    return text.translate(_ESCAPE_MAP)


# ---- offer line renderers ------------------------------------------------


def _format_offer_line(offer: Offer) -> str:
    title = _truncate(offer.title, 70)
    price = _format_price(offer.price)
    discount_suffix = ""
    if offer.price_original and offer.price_original > offer.price:
        pct = int((Decimal(1) - offer.price / offer.price_original) * 100)
        discount_suffix = f" <s>{_format_price(offer.price_original)}</s> (-{pct}%)"

    # hlink escapes the title it wraps; use _escape only for the no-link path.
    title_html = hlink(title, offer.deep_link) if offer.deep_link else _escape(title)
    stock_note = "" if offer.in_stock else " <i>(нет)</i>"
    return f"  • {title_html} — {hbold(price + ' ₽')}{discount_suffix}{stock_note}"


def _format_compare_line(label: str, offer: Offer) -> str:
    title = _truncate(offer.title, 60)
    # hlink escapes its own input. When there's no link we must escape
    # manually so raw "<" / "&" from the product name don't break the parse.
    title_html = hlink(title, offer.deep_link) if offer.deep_link else _escape(title)
    price = _format_price(offer.price)
    return f"{label}: {title_html} — {hbold(price + ' ₽')}"


# ---- /search rendering ---------------------------------------------------


def _format_search_results(query: str, results: list[SearchResult]) -> str:
    # hbold/hlink in aiogram do their own HTML escaping. Anywhere we route
    # user text through them, we pass the raw string — pre-escaping causes
    # double escapes (&amp;amp; etc). Raw text interpolated into f-strings
    # still has to be escaped explicitly.
    lines: list[str] = [f"🔎 {hbold(query)}\n"]
    total_offers = 0
    for result in results:
        label = _SERVICE_LABELS.get(result.service, result.service.value)
        if result.error:
            lines.append(f"{label}: <i>{_escape(_humanize_error(result.error))}</i>")
            lines.append("")
            continue
        if not result.offers:
            lines.append(f"{label}: <i>ничего не найдено</i>")
            lines.append("")
            continue

        lines.append(label)
        for offer in result.offers:
            total_offers += 1
            lines.append(_format_offer_line(offer))
        lines.append("")

    if total_offers == 0:
        lines.append("<i>Ни в одном сервисе не нашли — попробуй другой запрос.</i>")
    else:
        lines.append("<i>Нажми кнопку ниже, чтобы добавить товар в корзину.</i>")
    return "\n".join(lines).strip()


def _build_add_keyboard(token: str, results: list[SearchResult]) -> InlineKeyboardMarkup | None:
    """One row per service, up to 3 price buttons per row.

    Callback format: `AddToCart(token, idx)` — the flat index into the
    cached result list (same order as `SearchCache.put` flattens).
    """
    rows: list[list[InlineKeyboardButton]] = []
    flat_idx = 0
    for result in results:
        if result.error or not result.offers:
            # Still advance flat_idx? No — we don't cache failed offers,
            # so flat indices only cover good offers. See SearchCache.put.
            continue
        row: list[InlineKeyboardButton] = []
        for offer in result.offers:
            label = f"{_SERVICE_EMOJI[result.service]} {_format_price(offer.price)} ₽"
            row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=AddToCart(token=token, idx=flat_idx).pack(),
                )
            )
            flat_idx += 1
        if row:
            rows.append(row)
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---- /compare rendering + match-reduction helpers ------------------------


def _pick_reference_and_matches(
    results: list[SearchResult],
) -> tuple[Offer | None, list[MatchedOffer]]:
    """Pick a reference product and search for its counterparts in other services.

    We prefer VkusVill as the reference because its search quality is the
    most disciplined (tight keyword matching), then Auchan, then Metro.
    The reference within a service is the cheapest — if we can match that
    one across services, the user usually gets the most pragmatic answer.
    """
    priority = [Service.VKUSVILL, Service.AUCHAN, Service.METRO]
    by_service = {r.service: r for r in results if not r.error and r.offers}
    for service in priority:
        result = by_service.get(service)
        if result is None:
            continue
        reference = min(result.offers, key=lambda o: o.price)
        matches = find_matches(reference, results)
        if matches:
            return reference, matches
    return None, []


def _synthesize_matched_results(
    reference: Offer,
    matches: list[MatchedOffer],
    original: list[SearchResult],
) -> list[SearchResult]:
    """Build a compare-style result list where each service holds exactly
    its matched (or reference) offer. Used to feed the cache so the
    "add to cart" buttons point at the same items we display.

    Services that have no match are dropped from the list — their buttons
    would just confuse the user.
    """
    matched_by_service: dict[Service, Offer] = {m.service: m.offer for m in matches}
    matched_by_service[reference.service] = reference

    synthesized: list[SearchResult] = []
    for result in original:
        offer = matched_by_service.get(result.service)
        if offer is None:
            # Preserve shape with an errored/empty entry so index stays stable.
            synthesized.append(
                SearchResult(
                    query=result.query,
                    service=result.service,
                    offers=[],
                    error=result.error,
                )
            )
        else:
            synthesized.append(
                SearchResult(query=result.query, service=result.service, offers=[offer])
            )
    return synthesized


def _reduce_to_cheapest(results: list[SearchResult]) -> list[SearchResult]:
    """Return a new result list where each successful service holds only
    its single cheapest offer — the one highlighted by /compare.

    Errored / empty results are passed through so the cache + keyboard order
    lines up 1:1 with what the user sees in the text message.
    """
    reduced: list[SearchResult] = []
    for result in results:
        if result.error or not result.offers:
            reduced.append(result)
            continue
        cheapest = min(result.offers, key=lambda o: o.price)
        reduced.append(SearchResult(query=result.query, service=result.service, offers=[cheapest]))
    return reduced


def _build_compare_keyboard(token: str, results: list[SearchResult]) -> InlineKeyboardMarkup | None:
    """A single compact row — one button per service with an offer.

    /compare only ever shows one offer per service so this always fits
    on a single row (max 3 buttons). Callback schema matches the /search
    flow: the shared `AddToCart` handler picks the offer out of the cache
    by flat index.
    """
    buttons: list[InlineKeyboardButton] = []
    flat_idx = 0
    for result in results:
        if result.error or not result.offers:
            continue
        offer = result.offers[0]
        label = f"{_SERVICE_EMOJI[result.service]} {_format_price(offer.price)} ₽"
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=AddToCart(token=token, idx=flat_idx).pack(),
            )
        )
        flat_idx += 1
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _format_compare(query: str, results: list[SearchResult], *, matched: bool = False) -> str:
    """Render /compare's fallback view: cheapest per service, by price only.

    This runs when cross-service matching couldn't find an equivalent
    product. The footer warns the user that items may differ between
    services so they don't take the winner at face value.
    """
    lines: list[str] = [f"⚖️ {hbold(query)} — лучшее в каждом сервисе\n"]
    cheapest_overall: tuple[Service, Offer] | None = None

    for result in results:
        label = _SERVICE_LABELS.get(result.service, result.service.value)
        if result.error:
            lines.append(f"{label}: <i>{_escape(_humanize_error(result.error))}</i>")
            continue
        if not result.offers:
            lines.append(f"{label}: <i>нет</i>")
            continue
        best = min(result.offers, key=lambda o: o.price)
        lines.append(_format_compare_line(label, best))
        if cheapest_overall is None or best.price < cheapest_overall[1].price:
            cheapest_overall = (result.service, best)

    if cheapest_overall is not None:
        service, offer = cheapest_overall
        lines.append(
            f"\n🏆 Дешевле всего: {_SERVICE_LABELS[service]} — "
            f"{hbold(_format_price(offer.price) + ' ₽')}"
        )
        # Honest warning: without matching, items across services may not
        # be the same product. User should eyeball the titles.
        lines.append(
            "<i>⚠️ Товары в разных сервисах могут отличаться. "
            "Я не нашёл общего эквивалента — посмотри названия сам.</i>"
        )
        lines.append("<i>Нажми кнопку ниже — товар попадёт в корзину.</i>")
    else:
        lines.append("\n<i>Ничего не нашли ни в одном сервисе.</i>")
    return "\n".join(lines).strip()


def _format_matched_compare(query: str, reference: Offer, matches: list[MatchedOffer]) -> str:
    """Render /compare when we successfully matched a product across services.

    The output is tighter: one line per service with the matched offer,
    a confidence-ish note, and a clear winner.
    """
    lines: list[str] = [f"⚖️ {hbold(query)} — сопоставимые товары в разных сервисах\n"]

    # Group everything (reference + matches) under the matching product.
    entries: list[tuple[Service, Offer]] = [(reference.service, reference)]
    entries.extend((m.service, m.offer) for m in matches)
    # Order by our canonical service sequence so layout is stable.
    order = {s: i for i, s in enumerate(Service)}
    entries.sort(key=lambda pair: order.get(pair[0], 999))

    for service, offer in entries:
        label = _SERVICE_LABELS.get(service, service.value)
        lines.append(_format_compare_line(label, offer))

    winner = min(entries, key=lambda pair: pair[1].price)
    service, offer = winner
    lines.append(
        f"\n🏆 Дешевле всего: {_SERVICE_LABELS[service]} — "
        f"{hbold(_format_price(offer.price) + ' ₽')}"
    )
    # Show savings if they are meaningful (> 1 ₽).
    worst = max(entries, key=lambda pair: pair[1].price)
    savings = worst[1].price - offer.price
    if savings >= Decimal("1"):
        lines.append(
            f"<i>Экономия по сравнению с самым дорогим вариантом: {_format_price(savings)} ₽.</i>"
        )
    lines.append("<i>Нажми кнопку ниже — товар попадёт в корзину.</i>")
    return "\n".join(lines).strip()


# ---- /cart rendering -----------------------------------------------------


def _format_cart(
    groups: list[cart_repo.CartGroup],
) -> tuple[str, InlineKeyboardMarkup]:
    """Render /cart as text + inline keyboard.

    Each item gets a 4-button row [➖ / ×N / ➕ / 🗑]. Each service group
    gets a "Open in service" shortcut to the retailer's own cart page.
    A final row has "copy" and "clear".
    """
    lines: list[str] = ["🧺 " + hbold("Твоя корзина") + "\n"]
    grand_total = Decimal("0")
    rows: list[list[InlineKeyboardButton]] = []

    for group in groups:
        label = _SERVICE_LABELS.get(group.service, group.service.value)
        home = _SERVICE_HOME.get(group.service)
        header = hlink(label, home) if home else label
        lines.append(header)
        for item in group.items:
            qty = f" ×{item.quantity}" if item.quantity > 1 else ""
            link = item.deep_link
            title = _truncate(item.title, CART_TITLE_TRUNCATE)
            title_html = hlink(title, link) if link else _escape(title)
            price = _format_price(item.price)
            lines.append(f"  • {title_html} — {price} ₽{qty}")
            # One row per cart item: minus / count / plus / trash.
            qty_label = f"×{item.quantity}"
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➖",
                        callback_data=ChangeQty(op="-", item_id=item.id).pack(),
                    ),
                    InlineKeyboardButton(
                        text=qty_label,
                        callback_data=ChangeQty(op="?", item_id=item.id).pack(),
                    ),
                    InlineKeyboardButton(
                        text="➕",
                        callback_data=ChangeQty(op="+", item_id=item.id).pack(),
                    ),
                    InlineKeyboardButton(
                        text="🗑",
                        callback_data=RemoveItem(item_id=item.id).pack(),
                    ),
                ]
            )
        lines.append(f"  <b>Итого {label}: {_format_price(group.subtotal)} ₽</b>")
        # Shortcut row: open the service's own cart page to actually check out.
        cart_link = _SERVICE_CART_LINKS.get(group.service)
        if cart_link:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🛒 Открыть корзину {label[2:].strip()}",
                        url=cart_link,
                    )
                ]
            )
        lines.append("")
        grand_total += group.subtotal

    lines.append(f"💰 <b>Всего: {_format_price(grand_total)} ₽</b>")
    # Bottom action row.
    rows.append(
        [
            InlineKeyboardButton(
                text="📋 Скопировать",
                callback_data=CopyCart(action="list").pack(),
            ),
            InlineKeyboardButton(
                text="🧹 Очистить",
                callback_data=ClearCart(action="ask").pack(),
            ),
        ]
    )
    return "\n".join(lines).strip(), InlineKeyboardMarkup(inline_keyboard=rows)


def _format_cart_plaintext(groups: list[cart_repo.CartGroup]) -> str:
    """Render the cart as plain text suitable for copy/paste into notes."""
    lines: list[str] = ["🧺 Корзина"]
    grand_total = Decimal("0")
    for group in groups:
        label = _SERVICE_LABELS.get(group.service, group.service.value)
        lines.append("")
        lines.append(label)
        for item in group.items:
            qty = f" x{item.quantity}" if item.quantity > 1 else ""
            price = _format_price(item.price)
            lines.append(f"  - {item.title} — {price} ₽{qty}")
        lines.append(f"  Итого: {_format_price(group.subtotal)} ₽")
        grand_total += group.subtotal
    lines.append("")
    lines.append(f"Всего: {_format_price(grand_total)} ₽")
    return "\n".join(lines)
