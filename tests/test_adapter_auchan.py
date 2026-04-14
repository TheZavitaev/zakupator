"""Auchan adapter — JSON API."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from zakupator.adapters.auchan import AuchanAdapter
from zakupator.models import Service
from tests.conftest import FIXTURES_DIR, mock_client


async def test_parses_live_fixture(auchan_json, moscow_address):
    """Parse the captured real response end-to-end.

    The exact product list may churn whenever we refresh fixtures, so we
    assert on invariants (non-empty, sane types) rather than specific items.
    """
    adapter = AuchanAdapter(client=mock_client(auchan_json))
    try:
        result = await adapter.search("молоко простоквашино", moscow_address, limit=5)
    finally:
        await adapter.close()

    assert result.error is None
    assert len(result.offers) > 0
    for offer in result.offers:
        assert offer.service == Service.AUCHAN
        assert offer.title
        assert offer.price > 0
        assert offer.product_id


async def test_extracts_discount_when_present(auchan_json, moscow_address):
    """Find a discounted item in the fixture and verify old_price is parsed."""
    adapter = AuchanAdapter(client=mock_client(auchan_json))
    try:
        result = await adapter.search("молоко простоквашино", moscow_address, limit=10)
    finally:
        await adapter.close()

    # At least one offer from the live capture had oldPrice set. If the fixture
    # is refreshed and nobody's on sale, this can be relaxed — but while prices
    # change, Auchan's top results for popular goods reliably include promos.
    data = json.loads(auchan_json)
    raw_with_discount = [
        p for p in data["data"]["products"] if p.get("oldPrice") is not None
    ]
    if not raw_with_discount:
        pytest.skip("fixture has no discounted items — refresh capture when one lands")

    matched = [
        o for o in result.offers if o.price_original is not None
    ]
    assert matched, "at least one offer must have price_original"
    sample = matched[0]
    assert sample.price_original > sample.price
    assert isinstance(sample.price_original, Decimal)


async def test_deep_link_is_absolute(auchan_json, moscow_address):
    adapter = AuchanAdapter(client=mock_client(auchan_json))
    try:
        result = await adapter.search("молоко", moscow_address, limit=3)
    finally:
        await adapter.close()
    for offer in result.offers:
        if offer.deep_link:
            assert offer.deep_link.startswith("https://www.auchan.ru/")


async def test_http_error_is_surfaced(moscow_address):
    adapter = AuchanAdapter(client=mock_client(b'{"err":"no"}', status=500))
    try:
        result = await adapter.search("q", moscow_address, limit=5)
    finally:
        await adapter.close()
    assert result.offers == []
    assert result.error is not None
    assert "500" in result.error


async def test_malformed_json_surfaced_not_raised(moscow_address):
    adapter = AuchanAdapter(client=mock_client(b"not really json at all"))
    try:
        result = await adapter.search("q", moscow_address, limit=5)
    finally:
        await adapter.close()
    assert result.offers == []
    assert result.error is not None
