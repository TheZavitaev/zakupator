"""Data access functions for users, cart items, and search history.

Each function takes an AsyncSession so the caller controls transactions and
lifetimes — the functions themselves commit nothing implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zakupator.db import CartItem, SearchHistory, User
from zakupator.models import Offer, Service


@dataclass
class CartGroup:
    """A user's cart items for a single service, plus the subtotal."""

    service: Service
    items: list[CartItem]
    subtotal: Decimal


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None) -> User:
    """Find a User by telegram_id or insert a new row. Caller commits."""
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, username=username)
        session.add(user)
        await session.flush()  # populate user.id without committing
    elif username and user.username != username:
        user.username = username
    return user


async def add_cart_item(session: AsyncSession, user_id: int, offer: Offer) -> CartItem:
    """Add an offer to the user's cart.

    Simplification: if the exact (service, product_id) pair already exists,
    we bump its quantity instead of inserting a duplicate. This matches the
    natural mental model of "click twice → buy two".
    """
    existing = await session.execute(
        select(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.service == offer.service.value,
            CartItem.service_product_id == offer.product_id,
        )
    )
    item = existing.scalar_one_or_none()
    if item is not None:
        item.quantity += 1
        # Freshen price to what the user just saw — avoids the surprise of
        # the cart showing a stale price from the first time they added it.
        item.price = offer.price
        return item

    item = CartItem(
        user_id=user_id,
        service=offer.service.value,
        service_product_id=offer.product_id,
        title=offer.title,
        price=offer.price,
        quantity=1,
        deep_link=offer.deep_link,
    )
    session.add(item)
    await session.flush()
    return item


async def list_cart(session: AsyncSession, user_id: int) -> list[CartGroup]:
    """Return the user's cart grouped by service, ordered by our service enum."""
    result = await session.execute(
        select(CartItem)
        .where(CartItem.user_id == user_id)
        .order_by(CartItem.service, CartItem.added_at)
    )
    items = result.scalars().all()

    by_service: dict[str, list[CartItem]] = {}
    for item in items:
        by_service.setdefault(item.service, []).append(item)

    # Stable order across runs: follow the Service enum definition order.
    groups: list[CartGroup] = []
    for service in Service:
        bucket = by_service.get(service.value)
        if not bucket:
            continue
        subtotal = sum(
            (item.price * item.quantity for item in bucket),
            start=Decimal("0"),
        )
        groups.append(CartGroup(service=service, items=bucket, subtotal=subtotal))
    return groups


async def remove_cart_item(session: AsyncSession, user_id: int, item_id: int) -> bool:
    """Delete a single cart line. Returns True if something was removed."""
    result = await session.execute(
        delete(CartItem).where(CartItem.id == item_id, CartItem.user_id == user_id)
    )
    # CursorResult exposes rowcount; the async layer widens to Result in stubs.
    return (getattr(result, "rowcount", 0) or 0) > 0


async def change_quantity(
    session: AsyncSession, user_id: int, item_id: int, delta: int
) -> CartItem | None:
    """Bump or drop the quantity of a cart line.

    If the resulting quantity drops to zero or below, the row is deleted
    and None is returned so the caller can re-render the cart without it.

    Silently ignores items that don't belong to the given user — callers
    get None, matching the "not found" path.
    """
    result = await session.execute(
        select(CartItem).where(CartItem.id == item_id, CartItem.user_id == user_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        return None
    new_qty = item.quantity + delta
    if new_qty <= 0:
        await session.delete(item)
        return None
    item.quantity = new_qty
    return item


async def clear_cart(session: AsyncSession, user_id: int) -> int:
    """Wipe the user's cart. Returns the number of rows deleted."""
    result = await session.execute(delete(CartItem).where(CartItem.user_id == user_id))
    return int(getattr(result, "rowcount", 0) or 0)


async def record_search(session: AsyncSession, user_id: int, query: str) -> None:
    """Append a query to search history, collapsing immediate duplicates.

    If the user just searched for the same string, we don't insert another
    row — that would bloat the history with stuttering entries when they
    refine parameters or just retry.
    """
    recent = await session.execute(
        select(SearchHistory)
        .where(SearchHistory.user_id == user_id)
        .order_by(desc(SearchHistory.searched_at))
        .limit(1)
    )
    last = recent.scalar_one_or_none()
    if last is not None and last.query.strip().lower() == query.strip().lower():
        return
    session.add(SearchHistory(user_id=user_id, query=query))


async def list_recent_searches(session: AsyncSession, user_id: int, limit: int = 10) -> list[str]:
    """Return the user's most recent distinct queries, newest first."""
    # Subquery for max(searched_at) per query value, so we can order by
    # the *last* time each query was seen.
    subq = (
        select(
            SearchHistory.query,
            func.max(SearchHistory.searched_at).label("latest"),
        )
        .where(SearchHistory.user_id == user_id)
        .group_by(SearchHistory.query)
        .subquery()
    )
    result = await session.execute(select(subq.c.query).order_by(desc(subq.c.latest)).limit(limit))
    return [row[0] for row in result.all()]
