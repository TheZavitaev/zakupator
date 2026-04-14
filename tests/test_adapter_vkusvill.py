"""VkusVill adapter — HTML scraping."""

from __future__ import annotations

from decimal import Decimal

from tests.conftest import mock_client
from zakupator.adapters.vkusvill import VkusVillAdapter
from zakupator.models import Service


async def test_parses_three_cards_from_fixture(vkusvill_html, moscow_address):
    adapter = VkusVillAdapter(client=mock_client(vkusvill_html, content_type="text/html"))
    try:
        result = await adapter.search("молоко", moscow_address, limit=10)
    finally:
        await adapter.close()

    assert result.error is None, f"unexpected adapter error: {result.error}"
    assert len(result.offers) == 3, (
        "fixture has exactly 3 real product cards — header and footer links must not leak in"
    )
    assert all(o.service == Service.VKUSVILL for o in result.offers)


async def test_extracts_title_price_and_weight(vkusvill_html, moscow_address):
    adapter = VkusVillAdapter(client=mock_client(vkusvill_html, content_type="text/html"))
    try:
        result = await adapter.search("молоко", moscow_address, limit=10)
    finally:
        await adapter.close()

    first = result.offers[0]
    assert first.title == "Молоко 2,5% в бутылке, 900 мл"
    assert first.price == Decimal("100")
    assert first.price_original is None
    assert first.amount_unit == "900 мл"
    assert first.deep_link == ("https://vkusvill.ru/goods/moloko-2-5-v-butylke-900-ml-36296.html")
    assert first.product_id == "36296"


async def test_recognizes_discount(vkusvill_html, moscow_address):
    adapter = VkusVillAdapter(client=mock_client(vkusvill_html, content_type="text/html"))
    try:
        result = await adapter.search("молоко", moscow_address, limit=10)
    finally:
        await adapter.close()

    # The third fixture card is the discounted one: 282 ₽ (был 330).
    discounted = result.offers[2]
    assert discounted.title == "Масло сливочное 82,5%, 200 г"
    assert discounted.price == Decimal("282")
    assert discounted.price_original == Decimal("330")
    assert discounted.price_original > discounted.price


async def test_limit_honored(vkusvill_html, moscow_address):
    adapter = VkusVillAdapter(client=mock_client(vkusvill_html, content_type="text/html"))
    try:
        result = await adapter.search("молоко", moscow_address, limit=2)
    finally:
        await adapter.close()
    assert len(result.offers) == 2


async def test_http_error_returns_error_not_exception(moscow_address):
    adapter = VkusVillAdapter(
        client=mock_client(b"Service Unavailable", status=503, content_type="text/plain")
    )
    try:
        result = await adapter.search("молоко", moscow_address, limit=5)
    finally:
        await adapter.close()
    assert result.error is not None
    assert "503" in result.error
    assert result.offers == []


async def test_image_url_absolutized(vkusvill_html, moscow_address):
    adapter = VkusVillAdapter(client=mock_client(vkusvill_html, content_type="text/html"))
    try:
        result = await adapter.search("молоко", moscow_address, limit=10)
    finally:
        await adapter.close()

    # First card uses a relative src → should be absolutized to vkusvill.ru
    assert result.offers[0].image_url == "https://vkusvill.ru/upload/images/moloko-2-5.webp"
    # Second card uses an absolute cdn URL → keep as-is
    assert result.offers[1].image_url == "https://img.vkusvill.ru/cdn/moloko-3-2.webp"
    # Third uses data-src (lazy loading) → must fall back to that
    assert result.offers[2].image_url == "https://vkusvill.ru/upload/images/maslo.webp"
