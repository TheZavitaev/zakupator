"""Telegram bot — aiogram 3.

Commands:
  /start       — greet, ensure user record
  /help        — list commands
  /search <q>  — fan-out to all services, show results + "add to cart" keyboard
  /compare <q> — one-liner cheapest-per-service view
  /cart        — list cart items grouped by service, with subtotals
  /clear       — empty the cart (with confirmation)
  /history     — recent queries, clickable to re-run
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.markdown import hbold, hlink
from sqlalchemy.ext.asyncio import AsyncSession

from zakupator import cart_repo
from zakupator.callbacks import (
    AddToCart,
    ChangeQty,
    ClearCart,
    CopyCart,
    HistoryPick,
    RemoveItem,
    pack_history_pick,
)
from zakupator.config import Settings
from zakupator.constants import CART_TITLE_TRUNCATE, HISTORY_LIMIT
from zakupator.db import get_session_factory
from zakupator.matching import MatchedOffer, find_matches
from zakupator.middleware import DbSessionMiddleware
from zakupator.models import Address, Offer, SearchResult, Service
from zakupator.search import SearchEngine
from zakupator.search_cache import SearchCache

logger = logging.getLogger(__name__)

router = Router(name="zakupator")

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


# ---- /start, /help -------------------------------------------------------


@router.message(CommandStart())
async def on_start(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    await cart_repo.get_or_create_user(session, message.from_user.id, message.from_user.username)
    await message.answer(
        "Привет! Я сравниваю цены в ВкусВилле, Ашане и Metro.\n\n"
        "Просто напиши товар — например <code>молоко простоквашино</code> — "
        "и я покажу топ-3 в каждом сервисе.\n\n"
        "<b>Команды:</b>\n"
        "  /search &lt;что искать&gt; — то же самое, явно\n"
        "  /compare &lt;что искать&gt; — самое дешёвое в каждом сервисе одной строкой\n"
        "  /cart — твоя корзина (сгруппировано по сервисам)\n"
        "  /total — сводка по сервисам одной строкой\n"
        "  /clear — очистить корзину\n"
        "  /history — последние запросы\n",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(
        "<b>Команды:</b>\n"
        "  /search &lt;что искать&gt;\n"
        "  /compare &lt;что искать&gt;\n"
        "  /cart\n"
        "  /total\n"
        "  /clear\n"
        "  /history",
        parse_mode=ParseMode.HTML,
    )


# ---- /search -------------------------------------------------------------


@router.message(Command("search"))
async def on_search(
    message: Message,
    command: CommandObject,
    engine: SearchEngine,
    cache: SearchCache,
    default_address: Address,
    session: AsyncSession,
) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "Что искать? Например: <code>/search молоко простоквашино</code>\n\n"
            "Или просто напиши товар без команды.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _run_search(message, query, engine, cache, default_address, session)


# Plain text messages (not commands, not empty) are treated as a search query.
# Registered LAST so command handlers take precedence — aiogram dispatches in
# registration order and the first matching handler wins.
@router.message(F.text & ~F.text.startswith("/"))
async def on_plain_text(
    message: Message,
    engine: SearchEngine,
    cache: SearchCache,
    default_address: Address,
    session: AsyncSession,
) -> None:
    if message.text is None or message.from_user is None:
        return
    query = message.text.strip()
    if not query:
        return
    await _run_search(message, query, engine, cache, default_address, session)


async def _run_search(
    message: Message,
    query: str,
    engine: SearchEngine,
    cache: SearchCache,
    default_address: Address,
    session: AsyncSession,
) -> None:
    """Shared body for /search and plain-text search flows.

    Records the query in history, runs fan-out, formats the response, and
    attaches an "add to cart" inline keyboard.
    """
    if message.from_user is None:
        return
    user = await cart_repo.get_or_create_user(
        session, message.from_user.id, message.from_user.username
    )
    await cart_repo.record_search(session, user.id, query)

    progress = await message.answer(f"🔎 Ищу «{query}» в трёх сервисах…")
    try:
        results = await engine.search(query, default_address, limit_per_service=3)
    except Exception as e:
        logger.exception("search failed")
        await progress.edit_text(f"Ошибка при поиске: {type(e).__name__}: {e}")
        return

    entry = cache.put(query, results)
    text = _format_search_results(query, results)
    keyboard = _build_add_keyboard(entry.token, results)
    await progress.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


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

    Callback format: `a:<token>:<flat_idx>` — the flat index into the
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


@router.callback_query(AddToCart.filter())
async def on_add_to_cart(
    callback: CallbackQuery,
    callback_data: AddToCart,
    cache: SearchCache,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    token = callback_data.token
    idx = callback_data.idx

    entry = cache.get(token)
    if entry is None:
        await callback.answer(
            "Этот поиск устарел. Сделай новый /search",
            show_alert=True,
        )
        return

    if not 0 <= idx < len(entry.flat_offers):
        await callback.answer("Товар не найден", show_alert=False)
        return

    offer = entry.flat_offers[idx]
    user = await cart_repo.get_or_create_user(
        session, callback.from_user.id, callback.from_user.username
    )
    await cart_repo.add_cart_item(session, user.id, offer)

    label = _SERVICE_LABELS.get(offer.service, offer.service.value)
    toast = f"✓ В корзину: {offer.title[:40]} — {_format_price(offer.price)} ₽ ({label})"
    await callback.answer(toast, show_alert=False)


# ---- /compare ------------------------------------------------------------


@router.message(Command("compare"))
async def on_compare(
    message: Message,
    command: CommandObject,
    engine: SearchEngine,
    cache: SearchCache,
    default_address: Address,
    session: AsyncSession,
) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "Что сравниваем? Например: <code>/compare молоко простоквашино 930мл</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if message.from_user is None:
        return

    user = await cart_repo.get_or_create_user(
        session, message.from_user.id, message.from_user.username
    )
    await cart_repo.record_search(session, user.id, query)

    progress = await message.answer(f"⚖️ Сравниваю «{query}»…")

    try:
        results = await engine.search(query, default_address, limit_per_service=5)
    except Exception as e:
        logger.exception("compare failed")
        await progress.edit_text(f"Ошибка при поиске: {type(e).__name__}: {e}")
        return

    # Try to find matching products across services: pick a reference from
    # the first service that returned anything, then fuzzy-match the other
    # services' results against it. If we get at least one cross-match,
    # we show a "same product" block with confidence. Otherwise fall back
    # to the dumb per-service cheapest view.
    reference, matches = _pick_reference_and_matches(results)
    if reference is not None and matches:
        matched_results = _synthesize_matched_results(reference, matches, results)
        entry = cache.put(query, matched_results)
        keyboard = _build_compare_keyboard(entry.token, matched_results)
        text = _format_matched_compare(query, reference, matches)
    else:
        best_results = _reduce_to_cheapest(results)
        entry = cache.put(query, best_results)
        keyboard = _build_compare_keyboard(entry.token, best_results)
        text = _format_compare(query, results, matched=False)

    await progress.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


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
    on a single row (max 3 buttons). Callback format matches the /search
    flow: the shared `a:` handler picks the offer out of the cache by
    flat index.
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


def _format_compare_line(label: str, offer: Offer) -> str:
    title = _truncate(offer.title, 60)
    # hlink escapes its own input. When there's no link we must escape
    # manually so raw "<" / "&" from the product name don't break the parse.
    title_html = hlink(title, offer.deep_link) if offer.deep_link else _escape(title)
    price = _format_price(offer.price)
    return f"{label}: {title_html} — {hbold(price + ' ₽')}"


# ---- /cart ---------------------------------------------------------------


@router.message(Command("cart"))
async def on_cart(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    user = await cart_repo.get_or_create_user(
        session, message.from_user.id, message.from_user.username
    )
    groups = await cart_repo.list_cart(session, user.id)

    if not groups:
        await message.answer(
            "Корзина пуста. Сделай <code>/search</code> и нажми кнопку с ценой, "
            "чтобы добавить товар.",
            parse_mode=ParseMode.HTML,
        )
        return

    text, keyboard = _format_cart(groups)
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


def _format_cart(
    groups: list[cart_repo.CartGroup],
) -> tuple[str, InlineKeyboardMarkup]:
    """Render /cart as text + inline keyboard.

    Each item gets a 3-button row [➖ / title-hint / ➕] plus a row-wide
    remove button. Each service group gets a "Open in service" shortcut
    to the retailer's own cart page. A final row has "copy" and "clear".
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


@router.callback_query(ChangeQty.filter())
async def on_change_quantity(
    callback: CallbackQuery,
    callback_data: ChangeQty,
    session: AsyncSession,
) -> None:
    """Handle ➖ / ➕ / middle label clicks on cart item rows.

    The middle label (op == "?") is informational — we just surface the
    current quantity in a toast so the user has feedback if they tapped
    it by mistake.
    """
    if callback.from_user is None:
        await callback.answer()
        return
    op = callback_data.op
    item_id = callback_data.item_id

    user = await cart_repo.get_or_create_user(
        session, callback.from_user.id, callback.from_user.username
    )

    if op == "+":
        item = await cart_repo.change_quantity(session, user.id, item_id, 1)
        if item is None:
            await callback.answer("Товар больше не найден")
        else:
            await callback.answer(f"×{item.quantity}")
    elif op == "-":
        item = await cart_repo.change_quantity(session, user.id, item_id, -1)
        if item is None:
            await callback.answer("Удалено")
        else:
            await callback.answer(f"×{item.quantity}")
    else:
        # "?" middle label — just acknowledge.
        await callback.answer()
        return

    # Re-render cart to reflect the new quantity.
    await _rerender_cart(callback, session, user.id)


@router.callback_query(RemoveItem.filter())
async def on_remove_item(
    callback: CallbackQuery,
    callback_data: RemoveItem,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    item_id = callback_data.item_id

    user = await cart_repo.get_or_create_user(
        session, callback.from_user.id, callback.from_user.username
    )
    removed = await cart_repo.remove_cart_item(session, user.id, item_id)
    if not removed:
        await callback.answer("Уже удалено", show_alert=False)
        return

    await _rerender_cart(callback, session, user.id)
    await callback.answer("✓ Удалено", show_alert=False)


async def _rerender_cart(callback: CallbackQuery, session: AsyncSession, user_id: int) -> None:
    """Shared body that redraws the /cart message in place.

    Used by remove, quantity change, and potentially other mutations. If
    the message the callback is attached to was deleted or the cart went
    empty, we fall back to a plain "empty cart" text.
    """
    msg = callback.message
    if not isinstance(msg, Message):
        # None, or an InaccessibleMessage (too old to edit) — give up silently.
        return
    groups = await cart_repo.list_cart(session, user_id)
    if not groups:
        await msg.edit_text(
            "Корзина пуста. Сделай <code>/search</code> и добавь что-нибудь.",
            parse_mode=ParseMode.HTML,
        )
        return
    text, keyboard = _format_cart(groups)
    await msg.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


@router.callback_query(CopyCart.filter())
async def on_copy_cart(
    callback: CallbackQuery,
    callback_data: CopyCart,
    session: AsyncSession,
) -> None:
    """Send a plain-text dump of the cart in a separate message.

    Separate message (not an edit) so the user can easily copy from it —
    tapping a Telegram message selects its full text on all platforms.
    """
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    user = await cart_repo.get_or_create_user(
        session, callback.from_user.id, callback.from_user.username
    )
    groups = await cart_repo.list_cart(session, user.id)
    if not groups:
        await callback.answer("Корзина пуста")
        return

    text = _format_cart_plaintext(groups)
    # Plain text, not HTML, so Telegram doesn't mangle the output when copied.
    await callback.message.answer(text)
    await callback.answer("✓ Готово — тапни на сообщение, чтобы скопировать")


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


@router.message(Command("total"))
async def on_total(message: Message, session: AsyncSession) -> None:
    """One-liner cart summary: per-service subtotals + grand total."""
    if message.from_user is None:
        return
    user = await cart_repo.get_or_create_user(
        session, message.from_user.id, message.from_user.username
    )
    groups = await cart_repo.list_cart(session, user.id)
    if not groups:
        await message.answer("Корзина пуста.")
        return

    chunks = [f"{_SERVICE_EMOJI[g.service]} {_format_price(g.subtotal)} ₽" for g in groups]
    grand = sum((g.subtotal for g in groups), start=Decimal("0"))
    text = " · ".join(chunks) + f"\n💰 <b>{_format_price(grand)} ₽</b>"
    await message.answer(text, parse_mode=ParseMode.HTML)


# ---- /clear --------------------------------------------------------------


@router.message(Command("clear"))
async def on_clear(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    user = await cart_repo.get_or_create_user(
        session, message.from_user.id, message.from_user.username
    )
    groups = await cart_repo.list_cart(session, user.id)
    if not groups:
        await message.answer("Корзина и так пуста.")
        return

    total_items = sum(len(g.items) for g in groups)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, очистить",
                    callback_data=ClearCart(action="yes").pack(),
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=ClearCart(action="no").pack(),
                ),
            ]
        ]
    )
    await message.answer(
        f"Удалить все {total_items} позиций из корзины?",
        reply_markup=keyboard,
    )


@router.callback_query(ClearCart.filter())
async def on_clear_confirm(
    callback: CallbackQuery,
    callback_data: ClearCart,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    action = callback_data.action
    raw_msg = callback.message
    # Narrow out InaccessibleMessage so edit_text/answer are callable.
    msg = raw_msg if isinstance(raw_msg, Message) else None

    if action == "ask":
        # Triggered from the 🧹 button inside /cart.
        user = await cart_repo.get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )
        groups = await cart_repo.list_cart(session, user.id)
        if not groups:
            await callback.answer("Корзина пуста", show_alert=False)
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Да, очистить",
                        callback_data=ClearCart(action="yes").pack(),
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=ClearCart(action="no").pack(),
                    ),
                ]
            ]
        )
        if msg is not None:
            await msg.answer("Удалить всю корзину?", reply_markup=keyboard)
        await callback.answer()
        return

    if action == "no":
        if msg is not None:
            await msg.edit_text("Отменено.")
        await callback.answer()
        return

    if action == "yes":
        user = await cart_repo.get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )
        count = await cart_repo.clear_cart(session, user.id)
        if msg is not None:
            await msg.edit_text(f"Корзина очищена ({count} позиций удалено).")
        await callback.answer("✓ Очищено")
        return

    await callback.answer()


# ---- /history ------------------------------------------------------------


@router.message(Command("history"))
async def on_history(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    user = await cart_repo.get_or_create_user(
        session, message.from_user.id, message.from_user.username
    )
    queries = await cart_repo.list_recent_searches(session, user.id, limit=HISTORY_LIMIT)
    if not queries:
        await message.answer(
            "История пуста. Сделай первый <code>/search</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Show queries as buttons that re-run the search. We encode the query
    # directly in the callback_data — pack_history_pick trims byte-wise
    # to fit the 64-byte limit even for Cyrillic text.
    rows: list[list[InlineKeyboardButton]] = []
    for q in queries:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔎 {_truncate(q, 50)}",
                    callback_data=pack_history_pick(q),
                )
            ]
        )
    await message.answer(
        f"🕘 {hbold('Последние запросы')}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(HistoryPick.filter())
async def on_history_pick(
    callback: CallbackQuery,
    callback_data: HistoryPick,
    engine: SearchEngine,
    cache: SearchCache,
    default_address: Address,
    session: AsyncSession,
) -> None:
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    query = callback_data.query
    await callback.answer(f"🔎 {query}")

    user = await cart_repo.get_or_create_user(
        session, callback.from_user.id, callback.from_user.username
    )
    await cart_repo.record_search(session, user.id, query)

    progress = await callback.message.answer(f"🔎 Ищу «{query}» в трёх сервисах…")
    try:
        results = await engine.search(query, default_address, limit_per_service=3)
    except Exception as e:
        logger.exception("history-triggered search failed")
        await progress.edit_text(f"Ошибка: {type(e).__name__}: {e}")
        return

    entry = cache.put(query, results)
    text = _format_search_results(query, results)
    keyboard = _build_add_keyboard(entry.token, results)
    await progress.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


# ---- helpers -------------------------------------------------------------


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


# ---- wiring --------------------------------------------------------------


async def build_dispatcher(settings: Settings, engine: SearchEngine) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()

    default_address = Address(
        label=settings.default_address_label,
        text=settings.default_address_text,
        lat=settings.default_address_lat,
        lon=settings.default_address_lon,
    )
    cache = SearchCache()

    dp["engine"] = engine
    dp["cache"] = cache
    dp["default_address"] = default_address

    # DB session per update — commit/rollback transparently.
    session_middleware = DbSessionMiddleware(get_session_factory())
    dp.update.outer_middleware(session_middleware)

    dp.include_router(router)
    return bot, dp
