"""Shared data models for products, offers, and carts.

These are the common shapes every service adapter maps its raw response into.
Keeping them tiny and dumb on purpose - we'll evolve them as we learn what
real data from each service looks like.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Service(str, Enum):
    VKUSVILL = "vkusvill"
    AUCHAN = "auchan"
    METRO = "metro"


@dataclass(frozen=True)
class Address:
    """User's delivery address. lat/lon are what most services actually key on."""

    label: str  # human-readable, e.g. "Дом"
    text: str   # full address string
    lat: float
    lon: float


@dataclass
class Offer:
    """A specific product listing at a specific service at a specific moment."""

    service: Service
    product_id: str          # the service's own id
    title: str
    price: Decimal           # final price the user pays per unit
    price_original: Decimal | None = None  # before discount, if known
    unit: str | None = None  # "шт", "кг", "л"
    amount: float | None = None  # 0.930 for a 930ml bottle
    amount_unit: str | None = None  # "мл", "г", "шт"
    in_stock: bool = True
    image_url: str | None = None
    deep_link: str | None = None  # link back to the product in the service


@dataclass
class SearchResult:
    query: str
    service: Service
    offers: list[Offer] = field(default_factory=list)
    error: str | None = None  # non-fatal: "service unavailable", "no delivery here", etc
