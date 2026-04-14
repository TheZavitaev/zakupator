"""Metro adapter — GraphQL."""

from __future__ import annotations

import json

import pytest

from zakupator.adapters.metro import MetroAdapter
from zakupator.models import Service
from tests.conftest import mock_client


async def test_parses_live_fixture(metro_json, moscow_address):
    adapter = MetroAdapter(client=mock_client(metro_json))
    try:
        result = await adapter.search("молоко простоквашино", moscow_address, limit=5)
    finally:
        await adapter.close()

    assert result.error is None
    assert len(result.offers) > 0
    for offer in result.offers:
        assert offer.service == Service.METRO
        assert offer.title
        assert offer.price > 0
        assert offer.product_id


async def test_deep_link_absolute(metro_json, moscow_address):
    adapter = MetroAdapter(client=mock_client(metro_json))
    try:
        result = await adapter.search("молоко", moscow_address, limit=5)
    finally:
        await adapter.close()
    for offer in result.offers:
        if offer.deep_link:
            assert offer.deep_link.startswith("https://online.metro-cc.ru/")


async def test_gql_error_surfaced(moscow_address):
    body = json.dumps(
        {"errors": [{"message": "argument: storeId is required"}]}
    ).encode("utf-8")
    adapter = MetroAdapter(client=mock_client(body))
    try:
        result = await adapter.search("q", moscow_address, limit=5)
    finally:
        await adapter.close()
    assert result.offers == []
    assert result.error is not None
    assert "gql:" in result.error


async def test_http_error_surfaced(moscow_address):
    adapter = MetroAdapter(client=mock_client(b"nope", status=502))
    try:
        result = await adapter.search("q", moscow_address, limit=5)
    finally:
        await adapter.close()
    assert result.offers == []
    assert "502" in result.error


async def test_missing_stock_data_skipped(moscow_address):
    """A product without a valid stock entry should be dropped, not crash."""
    body = json.dumps(
        {
            "data": {
                "search": {
                    "products": {
                        "total": 2,
                        "products": [
                            {
                                "id": 1,
                                "article": 100,
                                "name": "no-stock product",
                                "url": "/products/broken",
                                "slug": "broken",
                                "images": [],
                                "manufacturer": {"name": "X"},
                                "stocks": [],
                            },
                            {
                                "id": 2,
                                "article": 101,
                                "name": "good product",
                                "url": "/products/good",
                                "slug": "good",
                                "images": ["img.jpg"],
                                "manufacturer": {"name": "X"},
                                "stocks": [
                                    {
                                        "store_id": 10,
                                        "eshop_availability": True,
                                        "value": 5.0,
                                        "text": "В наличии",
                                        "prices": {
                                            "price": 99.5,
                                            "old_price": None,
                                            "discount": 0,
                                            "is_promo": False,
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                }
            }
        }
    ).encode("utf-8")
    adapter = MetroAdapter(client=mock_client(body))
    try:
        result = await adapter.search("q", moscow_address, limit=5)
    finally:
        await adapter.close()
    assert len(result.offers) == 1
    assert result.offers[0].title == "good product"
