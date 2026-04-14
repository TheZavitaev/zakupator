"""SQLAlchemy models + session factory for user state.

Intentionally tiny for MVP: users, their saved addresses, and a single cart
per user. Cart items store a snapshot of the offer (price, title) so the cart
view is stable even if the underlying catalog churns.

One cart per user is an explicit simplification — we'll add named carts /
shopping lists later if the user asks for it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    addresses: Mapped[list[Address]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    cart_items: Mapped[list[CartItem]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    # When we add an "active address" feature, reintroduce a FK here with an
    # explicit `foreign_keys=[...]` on the Address relationship above so
    # SQLAlchemy can tell the two sides apart.


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(String(256))
    lat: Mapped[float] = mapped_column()
    lon: Mapped[float] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="addresses")


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    service: Mapped[str] = mapped_column(String(32))
    service_product_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    quantity: Mapped[int] = mapped_column(default=1)
    deep_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="cart_items")


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(String(200))
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ---- session plumbing ----

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str) -> None:
    # Module-level singletons on purpose — the bot has a single process-wide
    # engine and session factory, threaded into handlers via middleware.
    global _engine, _session_factory  # noqa: PLW0603
    _engine = create_async_engine(database_url, future=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("DB not initialized — call init_engine first")
    return _session_factory


async def create_all() -> None:
    if _engine is None:
        raise RuntimeError("DB not initialized")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
