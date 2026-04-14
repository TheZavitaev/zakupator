"""Bot text formatters — pure functions, easy to test.

These tests cover the price / truncation / escape helpers and the top-level
`_format_search_results` and `_format_compare` renderers. We don't spin up a
real aiogram Bot or Dispatcher — just call the private functions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from zakupator import cart_repo
from zakupator.bot import (
    _build_compare_keyboard,
    _escape,
    _format_cart,
    _format_cart_plaintext,
    _format_compare,
    _format_offer_line,
    _format_price,
    _format_search_results,
    _humanize_error,
    _reduce_to_cheapest,
    _truncate,
)
from zakupator.db import CartItem
from zakupator.models import Offer, SearchResult, Service


def _offer(
    service: Service,
    title: str,
    price: str,
    *,
    original: str | None = None,
    link: str | None = "https://example.com/x",
    in_stock: bool = True,
) -> Offer:
    return Offer(
        service=service,
        product_id=title,
        title=title,
        price=Decimal(price),
        price_original=Decimal(original) if original else None,
        in_stock=in_stock,
        deep_link=link,
    )


class TestFormatPrice:
    def test_trims_trailing_zero_decimals(self):
        assert _format_price(Decimal("97.00")) == "97"

    def test_keeps_meaningful_decimals(self):
        assert _format_price(Decimal("94.99")) == "94.99"

    def test_handles_integer(self):
        assert _format_price(Decimal("100")) == "100"

    def test_handles_zero(self):
        assert _format_price(Decimal("0")) == "0"


class TestTruncate:
    def test_short_text_untouched(self):
        assert _truncate("hi", 10) == "hi"

    def test_long_text_ellipsized(self):
        result = _truncate("a" * 100, 10)
        assert len(result) == 10
        assert result.endswith("…")


class TestEscape:
    def test_escapes_html_metacharacters(self):
        assert _escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"

    def test_plain_russian_text_untouched(self):
        assert _escape("Молоко") == "Молоко"


class TestFormatOfferLine:
    def test_includes_title_and_price(self):
        line = _format_offer_line(_offer(Service.AUCHAN, "Молоко", "94.99"))
        assert "Молоко" in line
        assert "94.99" in line
        assert "₽" in line

    def test_shows_discount_with_percentage(self):
        line = _format_offer_line(
            _offer(Service.AUCHAN, "X", "80", original="100")
        )
        assert "100" in line
        assert "80" in line
        assert "-20%" in line

    def test_out_of_stock_noted(self):
        line = _format_offer_line(_offer(Service.AUCHAN, "X", "100", in_stock=False))
        assert "нет" in line.lower()

    def test_html_escaped_in_title(self):
        line = _format_offer_line(
            _offer(Service.AUCHAN, "Tom & Jerry <brand>", "50")
        )
        assert "&amp;" in line
        assert "&lt;brand&gt;" in line
        assert "<brand>" not in line  # raw < never leaks


class TestFormatSearchResults:
    def test_shows_all_services_with_offers(self):
        results = [
            SearchResult(
                query="q",
                service=Service.VKUSVILL,
                offers=[_offer(Service.VKUSVILL, "v-milk", "100")],
            ),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "a-milk", "90")],
            ),
            SearchResult(
                query="q",
                service=Service.METRO,
                offers=[_offer(Service.METRO, "m-milk", "120")],
            ),
        ]
        text = _format_search_results("молоко", results)
        assert "ВкусВилл" in text
        assert "Ашан" in text
        assert "Metro" in text
        assert "v-milk" in text
        assert "a-milk" in text
        assert "m-milk" in text

    def test_empty_services_labeled(self):
        results = [
            SearchResult(query="q", service=Service.VKUSVILL, offers=[]),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "a", "10")],
            ),
            SearchResult(query="q", service=Service.METRO, error="http 500"),
        ]
        text = _format_search_results("молоко", results)
        assert "ничего не найдено" in text  # VkusVill
        # Metro error is humanized — raw "http 500" must NOT leak to the user.
        assert "http 500" not in text
        assert "недоступен" in text  # humanized form
        assert "a" in text  # Auchan offer still shown

    def test_all_empty_shows_fallback(self):
        results = [
            SearchResult(query="q", service=s, offers=[]) for s in Service
        ]
        text = _format_search_results("молоко", results)
        assert "не нашли" in text or "ничего" in text.lower()


class TestFormatCompare:
    def test_picks_cheapest_per_service(self):
        results = [
            SearchResult(
                query="q",
                service=Service.VKUSVILL,
                offers=[
                    _offer(Service.VKUSVILL, "expensive", "200"),
                    _offer(Service.VKUSVILL, "cheap", "100"),
                ],
            ),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "middle", "150")],
            ),
            SearchResult(
                query="q",
                service=Service.METRO,
                offers=[_offer(Service.METRO, "mid-pricey", "180")],
            ),
        ]
        text = _format_compare("молоко", results)
        # The VkusVill line should reference "cheap" (Decimal 100), not "expensive".
        assert "cheap" in text
        assert "expensive" not in text

    def test_highlights_cheapest_overall(self):
        results = [
            SearchResult(
                query="q",
                service=Service.VKUSVILL,
                offers=[_offer(Service.VKUSVILL, "v", "150")],
            ),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "a", "80")],
            ),
            SearchResult(
                query="q",
                service=Service.METRO,
                offers=[_offer(Service.METRO, "m", "120")],
            ),
        ]
        text = _format_compare("q", results)
        assert "Дешевле всего" in text
        assert "Ашан" in text
        # Should call out the winning price (80)
        assert "80" in text

    def test_all_empty_shows_not_found(self):
        results = [SearchResult(query="q", service=s, offers=[]) for s in Service]
        text = _format_compare("q", results)
        assert "не нашли" in text.lower() or "ничего" in text.lower()


class TestReduceToCheapest:
    def test_picks_min_price_per_service(self):
        results = [
            SearchResult(
                query="q",
                service=Service.VKUSVILL,
                offers=[
                    _offer(Service.VKUSVILL, "a", "200"),
                    _offer(Service.VKUSVILL, "b", "100"),
                    _offer(Service.VKUSVILL, "c", "150"),
                ],
            ),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "x", "90")],
            ),
        ]
        reduced = _reduce_to_cheapest(results)
        assert len(reduced) == 2
        assert len(reduced[0].offers) == 1
        assert reduced[0].offers[0].title == "b"
        assert len(reduced[1].offers) == 1
        assert reduced[1].offers[0].title == "x"

    def test_preserves_errors_and_empties(self):
        results = [
            SearchResult(query="q", service=Service.VKUSVILL, error="http 500"),
            SearchResult(query="q", service=Service.AUCHAN, offers=[]),
            SearchResult(
                query="q",
                service=Service.METRO,
                offers=[_offer(Service.METRO, "m", "100")],
            ),
        ]
        reduced = _reduce_to_cheapest(results)
        assert reduced[0].error == "http 500"
        assert reduced[1].offers == []
        assert len(reduced[2].offers) == 1


class TestBuildCompareKeyboard:
    def test_builds_one_row_with_one_button_per_successful_service(self):
        reduced = [
            SearchResult(
                query="q",
                service=Service.VKUSVILL,
                offers=[_offer(Service.VKUSVILL, "v", "100")],
            ),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "a", "88.99")],
            ),
            SearchResult(
                query="q",
                service=Service.METRO,
                offers=[_offer(Service.METRO, "m", "120")],
            ),
        ]
        keyboard = _build_compare_keyboard("tok123", reduced)
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 1, "must be a single row"
        row = keyboard.inline_keyboard[0]
        assert len(row) == 3
        # Each callback_data references a distinct flat index.
        assert row[0].callback_data == "a:tok123:0"
        assert row[1].callback_data == "a:tok123:1"
        assert row[2].callback_data == "a:tok123:2"
        # Labels show service emoji + price.
        assert "100" in row[0].text
        assert "88.99" in row[1].text
        assert "120" in row[2].text

    def test_skips_errored_and_empty_services_in_button_indices(self):
        reduced = [
            SearchResult(query="q", service=Service.VKUSVILL, error="boom"),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "a", "50")],
            ),
            SearchResult(query="q", service=Service.METRO, offers=[]),
        ]
        keyboard = _build_compare_keyboard("tok", reduced)
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 1
        row = keyboard.inline_keyboard[0]
        assert len(row) == 1
        # The button's flat index is 0 — the same index the cache will use
        # after flattening the errored entries out via SearchCache.put.
        assert row[0].callback_data == "a:tok:0"

    def test_returns_none_when_nothing_to_show(self):
        reduced = [
            SearchResult(query="q", service=s, offers=[]) for s in Service
        ]
        assert _build_compare_keyboard("tok", reduced) is None


class TestHumanizeError:
    def test_timeout(self):
        assert "вовремя" in _humanize_error("timeout")

    def test_network(self):
        assert "недоступен" in _humanize_error("network")

    def test_http_500(self):
        assert "недоступен" in _humanize_error("http 503")

    def test_http_429_rate_limit(self):
        assert "недоступен" in _humanize_error("http 429")

    def test_http_4xx(self):
        # 4xx (other than 429) — client-side issue, not "down"
        assert "недоступен" not in _humanize_error("http 404")

    def test_graphql(self):
        assert "ошибкой" in _humanize_error("gql: bad query")

    def test_non_json(self):
        assert "некорректный" in _humanize_error("non-json response")

    def test_empty(self):
        assert _humanize_error("") == "неизвестная ошибка"

    def test_unknown_code_passes_through_truncated(self):
        # Unknown error — still shown but without crashing.
        res = _humanize_error("something weird nobody mapped yet")
        assert len(res) <= 60
        assert res  # non-empty


def _cart_item(
    *, id: int, service: Service, title: str, price: str, qty: int = 1,
    deep_link: str | None = None,
) -> CartItem:
    item = CartItem(
        user_id=1,
        service=service.value,
        service_product_id=title,
        title=title,
        price=Decimal(price),
        quantity=qty,
        deep_link=deep_link,
    )
    item.id = id
    return item


def _cart_group(service: Service, *items: CartItem) -> cart_repo.CartGroup:
    subtotal = sum(
        (item.price * item.quantity for item in items), start=Decimal("0")
    )
    return cart_repo.CartGroup(service=service, items=list(items), subtotal=subtotal)


class TestFormatCart:
    def test_single_item_shows_quantity_buttons_and_remove(self):
        groups = [
            _cart_group(
                Service.VKUSVILL,
                _cart_item(id=1, service=Service.VKUSVILL, title="Молоко", price="100"),
            )
        ]
        text, keyboard = _format_cart(groups)
        assert "Молоко" in text
        assert "100" in text

        # First row: quantity controls for item 1.
        # We expect ➖ / ×N / ➕ / 🗑 on a single row.
        rows = keyboard.inline_keyboard
        qty_row = rows[0]
        assert len(qty_row) == 4
        labels = [b.text for b in qty_row]
        assert "➖" in labels
        assert "➕" in labels
        assert "🗑" in labels
        # Middle button shows current count
        assert any("×1" in lab for lab in labels)
        # Callback data points to item id 1
        assert any(b.callback_data == "q:-:1" for b in qty_row)
        assert any(b.callback_data == "q:+:1" for b in qty_row)
        assert any(b.callback_data == "r:1" for b in qty_row)

    def test_grand_total_matches_sum(self):
        groups = [
            _cart_group(
                Service.VKUSVILL,
                _cart_item(id=1, service=Service.VKUSVILL, title="a", price="100"),
                _cart_item(id=2, service=Service.VKUSVILL, title="b", price="50", qty=2),
            ),
            _cart_group(
                Service.AUCHAN,
                _cart_item(id=3, service=Service.AUCHAN, title="c", price="30"),
            ),
        ]
        text, _ = _format_cart(groups)
        # Subtotals: VkusVill 100 + 50*2 = 200, Auchan 30. Grand = 230.
        assert "230" in text
        assert "Всего" in text

    def test_copy_and_clear_buttons_always_present(self):
        groups = [
            _cart_group(
                Service.VKUSVILL,
                _cart_item(id=1, service=Service.VKUSVILL, title="x", price="1"),
            )
        ]
        _, keyboard = _format_cart(groups)
        bottom_row = keyboard.inline_keyboard[-1]
        labels = [b.text for b in bottom_row]
        assert any("Скопировать" in lab for lab in labels)
        assert any("Очистить" in lab for lab in labels)

    def test_service_cart_link_row_present(self):
        groups = [
            _cart_group(
                Service.AUCHAN,
                _cart_item(id=1, service=Service.AUCHAN, title="x", price="1"),
            )
        ]
        _, keyboard = _format_cart(groups)
        # Find the row with a URL button (not callback_data).
        url_rows = [
            row for row in keyboard.inline_keyboard
            if any(b.url for b in row)
        ]
        assert url_rows, "must have at least one 'open in service' link row"
        assert any(
            "auchan.ru" in b.url for row in url_rows for b in row if b.url
        )


class TestFormatCartPlaintext:
    def test_plain_no_html_tags(self):
        groups = [
            _cart_group(
                Service.VKUSVILL,
                _cart_item(id=1, service=Service.VKUSVILL, title="Молоко", price="100"),
                _cart_item(id=2, service=Service.VKUSVILL, title="Хлеб", price="50", qty=2),
            ),
            _cart_group(
                Service.AUCHAN,
                _cart_item(id=3, service=Service.AUCHAN, title="Яйца", price="80"),
            ),
        ]
        text = _format_cart_plaintext(groups)
        assert "<" not in text
        assert ">" not in text
        # All titles present.
        assert "Молоко" in text
        assert "Хлеб" in text
        assert "Яйца" in text
        # Grand total: 100 + 50*2 + 80 = 280
        assert "280" in text
        assert "x2" in text  # quantity noted
