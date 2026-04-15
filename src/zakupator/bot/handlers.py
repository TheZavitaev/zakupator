"""Telegram command and callback handlers for zakupator.

This module owns the aiogram `Router` and `build_dispatcher` entry point.
All text/keyboard rendering is delegated to `bot.presentation` so this
file stays focused on side effects: DB reads/writes, Telegram API calls,
and the flow between /search → /cart → /clear.

Commands:
  /start       — greet, ensure user record
  /help        — list commands
  /search <q>  — fan-out to all services, show results + "add to cart" keyboard
  /compare <q> — one-liner cheapest-per-service view
  /cart        — list cart items grouped by service, with subtotals
  /total       — per-service subtotals + grand total, one line
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
from aiogram.utils.markdown import hbold
from sqlalchemy.ext.asyncio import AsyncSession

from zakupator import cart_repo
from zakupator.bot.presentation import (
    _SERVICE_EMOJI,
    _SERVICE_LABELS,
    _build_add_keyboard,
    _build_compare_keyboard,
    _format_cart,
    _format_cart_plaintext,
    _format_compare,
    _format_matched_compare,
    _format_price,
    _format_search_results,
    _pick_reference_and_matches,
    _reduce_to_cheapest,
    _synthesize_matched_results,
    _truncate,
)
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
from zakupator.constants import HISTORY_LIMIT
from zakupator.db import get_session_factory
from zakupator.middleware import DbSessionMiddleware
from zakupator.models import Address
from zakupator.search import SearchEngine
from zakupator.search_cache import SearchCache

logger = logging.getLogger(__name__)

router = Router(name="zakupator")


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
