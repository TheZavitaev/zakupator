"""cart_repo — CRUD over cart items and search history."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zakupator import cart_repo
from zakupator.db import Base
from zakupator.models import Offer, Service


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Fresh in-memory SQLite DB per test.

    shared-cache URI so SQLite treats multiple connections as one DB,
    then `expire_on_commit=False` so we can read objects after commit.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _offer(
    service: Service = Service.AUCHAN,
    pid: str = "1",
    title: str = "Молоко",
    price: str = "100",
) -> Offer:
    return Offer(
        service=service,
        product_id=pid,
        title=title,
        price=Decimal(price),
        deep_link=f"https://example.com/{pid}",
    )


async def test_get_or_create_user_idempotent(session):
    u1 = await cart_repo.get_or_create_user(session, 42, "alice")
    await session.commit()
    u2 = await cart_repo.get_or_create_user(session, 42, "alice")
    assert u1.id == u2.id


async def test_get_or_create_user_updates_username(session):
    await cart_repo.get_or_create_user(session, 42, "alice")
    await session.commit()
    u2 = await cart_repo.get_or_create_user(session, 42, "bob")
    assert u2.username == "bob"


async def test_add_cart_item_persists_snapshot(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.add_cart_item(
        session, user.id, _offer(title="Молоко Простоквашино", price="88.99")
    )
    await session.commit()

    groups = await cart_repo.list_cart(session, user.id)
    assert len(groups) == 1
    assert groups[0].service == Service.AUCHAN
    assert len(groups[0].items) == 1
    item = groups[0].items[0]
    assert item.title == "Молоко Простоквашино"
    assert item.price == Decimal("88.99")
    assert groups[0].subtotal == Decimal("88.99")


async def test_adding_same_item_bumps_quantity(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.add_cart_item(session, user.id, _offer(pid="42", price="100"))
    await cart_repo.add_cart_item(session, user.id, _offer(pid="42", price="100"))
    await session.commit()

    groups = await cart_repo.list_cart(session, user.id)
    assert len(groups) == 1
    assert len(groups[0].items) == 1
    assert groups[0].items[0].quantity == 2
    assert groups[0].subtotal == Decimal("200")


async def test_adding_same_item_refreshes_price(session):
    """If price changed between two clicks, show the latest one."""
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.add_cart_item(session, user.id, _offer(pid="1", price="100"))
    await cart_repo.add_cart_item(session, user.id, _offer(pid="1", price="110"))
    await session.commit()

    groups = await cart_repo.list_cart(session, user.id)
    assert groups[0].items[0].price == Decimal("110")


async def test_list_cart_groups_by_service_in_enum_order(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    # Add in non-enum order — group order should still match Service enum.
    await cart_repo.add_cart_item(session, user.id, _offer(Service.METRO, "m1", "мясо", "300"))
    await cart_repo.add_cart_item(session, user.id, _offer(Service.VKUSVILL, "v1", "ягоды", "150"))
    await cart_repo.add_cart_item(session, user.id, _offer(Service.AUCHAN, "a1", "яйца", "80"))
    await session.commit()

    groups = await cart_repo.list_cart(session, user.id)
    assert [g.service for g in groups] == [Service.VKUSVILL, Service.AUCHAN, Service.METRO]


async def test_remove_cart_item_returns_true_on_hit(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.add_cart_item(session, user.id, _offer(pid="1"))
    await session.commit()

    groups = await cart_repo.list_cart(session, user.id)
    item_id = groups[0].items[0].id
    removed = await cart_repo.remove_cart_item(session, user.id, item_id)
    assert removed is True
    groups = await cart_repo.list_cart(session, user.id)
    assert groups == []


async def test_remove_cart_item_ignores_other_users_items(session):
    alice = await cart_repo.get_or_create_user(session, 1, "alice")
    bob = await cart_repo.get_or_create_user(session, 2, "bob")
    await cart_repo.add_cart_item(session, alice.id, _offer(pid="1"))
    await session.commit()

    alice_item_id = (await cart_repo.list_cart(session, alice.id))[0].items[0].id
    # Bob tries to delete Alice's item — must not succeed.
    removed = await cart_repo.remove_cart_item(session, bob.id, alice_item_id)
    assert removed is False
    # Alice's item is still there.
    groups = await cart_repo.list_cart(session, alice.id)
    assert len(groups) == 1


async def test_change_quantity_bumps_up(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.add_cart_item(session, user.id, _offer(pid="1"))
    await session.commit()

    item_id = (await cart_repo.list_cart(session, user.id))[0].items[0].id
    updated = await cart_repo.change_quantity(session, user.id, item_id, 1)
    assert updated is not None
    assert updated.quantity == 2


async def test_change_quantity_drops_down_and_deletes_at_zero(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.add_cart_item(session, user.id, _offer(pid="1"))
    await session.commit()

    item_id = (await cart_repo.list_cart(session, user.id))[0].items[0].id
    # quantity is 1, -1 should delete the row entirely
    result = await cart_repo.change_quantity(session, user.id, item_id, -1)
    assert result is None
    assert await cart_repo.list_cart(session, user.id) == []


async def test_change_quantity_ignores_other_users_items(session):
    alice = await cart_repo.get_or_create_user(session, 1, "alice")
    bob = await cart_repo.get_or_create_user(session, 2, "bob")
    await cart_repo.add_cart_item(session, alice.id, _offer(pid="1"))
    await session.commit()

    alice_item_id = (await cart_repo.list_cart(session, alice.id))[0].items[0].id
    # Bob tries to mutate Alice's item.
    result = await cart_repo.change_quantity(session, bob.id, alice_item_id, 5)
    assert result is None
    # Alice's quantity unchanged.
    alice_groups = await cart_repo.list_cart(session, alice.id)
    assert alice_groups[0].items[0].quantity == 1


async def test_change_quantity_nonexistent_returns_none(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    result = await cart_repo.change_quantity(session, user.id, 999, 1)
    assert result is None


async def test_clear_cart_wipes_all_items(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    for i in range(3):
        await cart_repo.add_cart_item(session, user.id, _offer(pid=str(i)))
    await session.commit()

    count = await cart_repo.clear_cart(session, user.id)
    assert count == 3
    assert await cart_repo.list_cart(session, user.id) == []


async def test_search_history_records_and_dedups(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.record_search(session, user.id, "молоко")
    await cart_repo.record_search(session, user.id, "молоко")  # dup — skipped
    await cart_repo.record_search(session, user.id, "хлеб")
    await cart_repo.record_search(session, user.id, "молоко")  # not a dup of last
    await session.commit()

    recent = await cart_repo.list_recent_searches(session, user.id, limit=10)
    # Most recent first, distinct, "молоко" should be first (it was re-queried).
    assert recent[0] == "молоко"
    assert "хлеб" in recent
    assert len(recent) == 2, "distinct by query value"


async def test_search_history_case_insensitive_dedup(session):
    user = await cart_repo.get_or_create_user(session, 1, "u")
    await cart_repo.record_search(session, user.id, "Молоко")
    await cart_repo.record_search(session, user.id, "  МОЛОКО  ")
    await session.commit()

    recent = await cart_repo.list_recent_searches(session, user.id, limit=10)
    # Second call should be skipped since it normalises to the same thing.
    assert len(recent) == 1
