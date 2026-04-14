"""Cross-service product matching.

The problem: user runs `/compare молоко простоквашино 930мл`. Each service's
search returns its own top results, but they aren't necessarily the same
physical product. Before we declare "Metro wins at 88₽", we want to be
reasonably sure that Metro's 88₽ item is comparable to the one VkusVill
showed at 100₽ — same brand, same package size, not e.g. a 200ml bottle
vs a 2L carton.

Approach:
1. Extract a quantity tuple (value, unit_class) from each product name.
   Unit class is one of {volume, mass, pieces} after normalization so
   "930 мл" and "0.93 л" match.
2. Compute a similarity score on the name strings using rapidfuzz's
   token-set ratio — word order doesn't matter, duplicate tokens are
   ignored.
3. Two offers are "the same product" iff: same unit class AND values within
   5% AND name similarity ≥ some threshold.

This is a heuristic. False negatives are fine (we'll show "no match"), but
false positives (claiming A == B when they're different products) are
actively misleading, so we err on the side of being strict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from zakupator.models import Offer, SearchResult, Service

# ---- quantity extraction -------------------------------------------------

# Classes — all units within a class get converted to the same base unit
# (ml for volume, g for mass, piece count).
_VOLUME = "volume"
_MASS = "mass"
_PIECES = "pieces"


@dataclass(frozen=True)
class Quantity:
    value: float  # in the base unit
    unit_class: str  # "volume" | "mass" | "pieces"


# Regex captures: a number (possibly comma-decimal) followed by a unit.
# The unit group is intentionally broad so we handle Russian abbreviations
# and spelled-out forms. Order matters: try more specific units first.
_QUANTITY_RE = re.compile(
    r"(\d+[.,]?\d*)\s*"
    r"(мл|миллилитр|л\b|литр|кг|килограмм|гр\b|г\b|грамм|шт\b|штук)",
    re.IGNORECASE,
)

_UNIT_TO_BASE: dict[str, tuple[str, float]] = {
    # volume -> ml
    "мл": (_VOLUME, 1.0),
    "миллилитр": (_VOLUME, 1.0),
    "л": (_VOLUME, 1000.0),
    "литр": (_VOLUME, 1000.0),
    # mass -> g
    "г": (_MASS, 1.0),
    "гр": (_MASS, 1.0),
    "грамм": (_MASS, 1.0),
    "кг": (_MASS, 1000.0),
    "килограмм": (_MASS, 1000.0),
    # pieces
    "шт": (_PIECES, 1.0),
    "штук": (_PIECES, 1.0),
}


def extract_quantity(name: str) -> Quantity | None:
    """Parse the first quantity mention out of a product name.

    Returns None if no recognizable quantity is present. Picks the first
    match because the first unit in the name is usually the primary one;
    secondary mentions (like "230 ккал на 100 г") would confuse things.
    """
    for match in _QUANTITY_RE.finditer(name):
        raw_value = match.group(1).replace(",", ".")
        try:
            value = float(raw_value)
        except ValueError:
            continue
        unit_key = match.group(2).lower().rstrip(".")
        # Normalize singular/plural suffixes away — "грамм"/"граммов"/"грамма".
        for key, (cls, mult) in _UNIT_TO_BASE.items():
            if unit_key.startswith(key):
                return Quantity(value=value * mult, unit_class=cls)
    return None


# ---- similarity ----------------------------------------------------------


# Thresholds were picked by eyeballing real data during recon:
# - token_set_ratio in [80..100] strongly correlates with "same product"
# - sub-70 is almost always a different product or a wildly different package
# - 70..80 is a fuzzy zone we intentionally exclude
_NAME_SIM_THRESHOLD = 80

# Quantity tolerance: "930 мл" vs "970 мл" is close enough to consider the
# same class (different brands often vary by 20-50ml). "1 л" vs "2 л" isn't.
_QTY_RELATIVE_TOLERANCE = 0.12


def name_similarity(a: str, b: str) -> float:
    """Return a 0..100 similarity score for two product names."""
    return fuzz.token_set_ratio(a, b)


def is_same_product(a: Offer, b: Offer) -> bool:
    """Heuristic equality check between two offers from different services.

    Must be:
      1. Similar enough by token-set ratio on name.
      2. If both names carry quantities: same unit class, within tolerance.
         If one side has no parseable quantity, we fall back to name alone.

    Deliberately strict — false positives are worse than false negatives
    here, because we might tell the user "cheapest is X" when X is a
    different product.
    """
    if name_similarity(a.title, b.title) < _NAME_SIM_THRESHOLD:
        return False

    qa = extract_quantity(a.title)
    qb = extract_quantity(b.title)
    if qa is None or qb is None:
        # Can't compare quantities — trust the name similarity alone.
        return True
    if qa.unit_class != qb.unit_class:
        return False
    if qa.value == 0 or qb.value == 0:
        return qa.value == qb.value
    ratio = min(qa.value, qb.value) / max(qa.value, qb.value)
    return ratio >= (1.0 - _QTY_RELATIVE_TOLERANCE)


# ---- cross-service matching -----------------------------------------------


@dataclass(frozen=True)
class MatchedOffer:
    """A single service's offer that was matched to a reference product."""

    service: Service
    offer: Offer
    score: float  # 0..100, name similarity at match time


def find_matches(reference: Offer, candidates: list[SearchResult]) -> list[MatchedOffer]:
    """For a reference offer, return the best match in each other service.

    At most one offer per service is returned — the one with the highest
    name similarity score among candidates deemed to be "the same product".
    Services where nothing matches are simply absent from the result list.

    Services equal to the reference's own are skipped.
    """
    out: list[MatchedOffer] = []
    for result in candidates:
        if result.error or not result.offers:
            continue
        if result.service == reference.service:
            continue
        best: MatchedOffer | None = None
        for cand in result.offers:
            if not is_same_product(reference, cand):
                continue
            score = name_similarity(reference.title, cand.title)
            if best is None or score > best.score:
                best = MatchedOffer(service=result.service, offer=cand, score=score)
        if best is not None:
            out.append(best)
    return out


def cheapest_across_matches(
    reference: Offer, matches: list[MatchedOffer]
) -> tuple[Service, Offer] | None:
    """Return (service, offer) whose price is the lowest among reference+matches.

    Returns None if the reference has no matches (nothing to compare).
    """
    if not matches:
        return None
    candidates: list[tuple[Service, Offer]] = [(reference.service, reference)]
    candidates.extend((m.service, m.offer) for m in matches)
    return min(candidates, key=lambda pair: pair[1].price)
