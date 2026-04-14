"""Cross-service product matching."""

from __future__ import annotations

from decimal import Decimal

from zakupator.matching import (
    MatchedOffer,
    cheapest_across_matches,
    extract_quantity,
    find_matches,
    is_same_product,
    name_similarity,
)
from zakupator.models import Offer, SearchResult, Service


def _offer(
    service: Service,
    title: str,
    price: str = "100",
) -> Offer:
    return Offer(
        service=service,
        product_id=title,
        title=title,
        price=Decimal(price),
    )


class TestExtractQuantity:
    def test_milliliters(self):
        q = extract_quantity("Молоко Простоквашино 2,5%, 930 мл")
        assert q is not None
        assert q.unit_class == "volume"
        assert q.value == 930

    def test_liters_converted_to_ml(self):
        q = extract_quantity("Сок яблочный 1 л")
        assert q.unit_class == "volume"
        assert q.value == 1000

    def test_grams(self):
        q = extract_quantity("Сыр Маасдам 200 г")
        assert q.unit_class == "mass"
        assert q.value == 200

    def test_kilograms_converted_to_g(self):
        q = extract_quantity("Мясо 1.5 кг")
        assert q.unit_class == "mass"
        assert q.value == 1500

    def test_pieces(self):
        q = extract_quantity("Яйца куриные С1, 10 шт")
        assert q.unit_class == "pieces"
        assert q.value == 10

    def test_no_unit_returns_none(self):
        assert extract_quantity("Молоко цельное") is None

    def test_comma_decimal_handled(self):
        q = extract_quantity("Масло 0,5 л")
        assert q.value == 500

    def test_first_match_wins(self):
        # A product description might have "100 г" and "930 мл" — we want
        # whichever comes first as the primary size marker.
        q = extract_quantity("Творог 5% 230 г (жиров 5 г на 100 г)")
        assert q.value == 230


class TestNameSimilarity:
    def test_identical(self):
        assert name_similarity("Молоко", "Молоко") == 100

    def test_word_order_invariant(self):
        a = "Молоко Простоквашино 2,5% 930 мл"
        b = "Простоквашино Молоко 930 мл 2,5%"
        assert name_similarity(a, b) >= 90

    def test_different_products_low(self):
        assert name_similarity("Молоко", "Хлеб") < 50


class TestIsSameProduct:
    def test_same_title_same_quantity(self):
        a = _offer(Service.VKUSVILL, "Молоко Простоквашино 2,5% 930 мл")
        b = _offer(Service.AUCHAN, "Молоко Простоквашино 2.5% 930 мл")
        assert is_same_product(a, b)

    def test_similar_title_similar_quantity(self):
        # 930 vs 970 mL — close enough under the tolerance.
        a = _offer(Service.VKUSVILL, "Молоко Простоквашино пастер. 2,5% 930 мл")
        b = _offer(Service.METRO, "Молоко Простоквашино 2,5%, 970мл")
        assert is_same_product(a, b)

    def test_different_volume_rejected(self):
        a = _offer(Service.VKUSVILL, "Молоко Простоквашино 2,5% 930 мл")
        b = _offer(Service.AUCHAN, "Молоко Простоквашино 2,5% 2 л")
        assert not is_same_product(a, b)

    def test_different_unit_class_rejected(self):
        a = _offer(Service.VKUSVILL, "Молоко Простоквашино 930 мл")
        b = _offer(Service.AUCHAN, "Молоко Простоквашино 930 г")
        assert not is_same_product(a, b)

    def test_completely_different_product(self):
        a = _offer(Service.VKUSVILL, "Молоко Простоквашино 930 мл")
        b = _offer(Service.AUCHAN, "Хлеб бородинский 500 г")
        assert not is_same_product(a, b)

    def test_similar_name_no_quantities_accepted(self):
        # If neither side has quantities, name similarity alone is enough.
        a = _offer(Service.VKUSVILL, "Молоко Простоквашино пастеризованное")
        b = _offer(Service.AUCHAN, "Простоквашино молоко пастеризованное")
        assert is_same_product(a, b)

    def test_similar_quantities_but_name_too_different(self):
        a = _offer(Service.VKUSVILL, "Молоко Простоквашино 930 мл")
        b = _offer(Service.AUCHAN, "Йогурт питьевой 930 мл")
        assert not is_same_product(a, b)


class TestFindMatches:
    def test_finds_best_match_in_each_other_service(self):
        reference = _offer(Service.VKUSVILL, "Молоко Простоквашино 2,5% 930 мл", "100")
        candidates = [
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[
                    _offer(Service.AUCHAN, "Сыр Российский 500 г", "300"),
                    _offer(Service.AUCHAN, "Молоко Простоквашино 2,5% 930 мл", "88"),
                    _offer(Service.AUCHAN, "Молоко Простоквашино 1,5% 930 мл", "85"),
                ],
            ),
            SearchResult(
                query="q",
                service=Service.METRO,
                offers=[
                    _offer(Service.METRO, "Молоко Простоквашино 2,5%, 970 мл", "149"),
                ],
            ),
        ]
        matches = find_matches(reference, candidates)
        assert len(matches) == 2
        by_service = {m.service: m for m in matches}
        # Auchan match should prefer the 2.5% variant (higher name similarity).
        auchan = by_service[Service.AUCHAN]
        assert "2,5%" in auchan.offer.title
        assert auchan.offer.price == Decimal("88")
        # Metro match is the only candidate.
        metro = by_service[Service.METRO]
        assert metro.offer.price == Decimal("149")

    def test_skips_reference_service(self):
        reference = _offer(Service.VKUSVILL, "Молоко 930 мл", "100")
        candidates = [
            SearchResult(
                query="q",
                service=Service.VKUSVILL,  # same service as reference
                offers=[_offer(Service.VKUSVILL, "Молоко 930 мл", "50")],
            ),
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "Молоко 930 мл", "88")],
            ),
        ]
        matches = find_matches(reference, candidates)
        services = {m.service for m in matches}
        assert Service.VKUSVILL not in services
        assert Service.AUCHAN in services

    def test_no_matches_when_nothing_similar(self):
        reference = _offer(Service.VKUSVILL, "Мёд липовый 500 г", "300")
        candidates = [
            SearchResult(
                query="q",
                service=Service.AUCHAN,
                offers=[_offer(Service.AUCHAN, "Хлеб бородинский 400 г", "50")],
            ),
        ]
        assert find_matches(reference, candidates) == []

    def test_errored_candidate_results_skipped(self):
        reference = _offer(Service.VKUSVILL, "Молоко 930 мл", "100")
        candidates = [
            SearchResult(query="q", service=Service.AUCHAN, error="timeout"),
            SearchResult(query="q", service=Service.METRO, offers=[]),
        ]
        assert find_matches(reference, candidates) == []


class TestCheapestAcrossMatches:
    def test_picks_minimum_including_reference(self):
        reference = _offer(Service.VKUSVILL, "Молоко", "100")
        matches = [
            MatchedOffer(
                service=Service.AUCHAN,
                offer=_offer(Service.AUCHAN, "Молоко", "88"),
                score=95,
            ),
            MatchedOffer(
                service=Service.METRO,
                offer=_offer(Service.METRO, "Молоко", "120"),
                score=90,
            ),
        ]
        service, offer = cheapest_across_matches(reference, matches)
        assert service == Service.AUCHAN
        assert offer.price == Decimal("88")

    def test_reference_wins_when_cheapest(self):
        reference = _offer(Service.VKUSVILL, "Молоко", "50")
        matches = [
            MatchedOffer(
                service=Service.AUCHAN,
                offer=_offer(Service.AUCHAN, "Молоко", "88"),
                score=95,
            ),
        ]
        service, offer = cheapest_across_matches(reference, matches)
        assert service == Service.VKUSVILL
        assert offer.price == Decimal("50")

    def test_no_matches_returns_none(self):
        reference = _offer(Service.VKUSVILL, "Молоко", "50")
        assert cheapest_across_matches(reference, []) is None
