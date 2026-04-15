"""Telegram bot package.

Re-exports the public entry point (`build_dispatcher`) and the `router`
so existing imports like `from zakupator.bot import build_dispatcher`
keep working after the split into `presentation` + `handlers`.

Private formatter/keyboard symbols are also re-exported because the
test suite in `tests/test_bot_formatters.py` reaches into them directly
— they're the pure functions that actually deserve coverage, and it's
more honest to expose them at the old path than to duplicate them.
"""

from __future__ import annotations

from zakupator.bot.handlers import build_dispatcher, router
from zakupator.bot.presentation import (
    _build_add_keyboard,
    _build_compare_keyboard,
    _escape,
    _format_cart,
    _format_cart_plaintext,
    _format_compare,
    _format_matched_compare,
    _format_offer_line,
    _format_price,
    _format_search_results,
    _humanize_error,
    _pick_reference_and_matches,
    _reduce_to_cheapest,
    _synthesize_matched_results,
    _truncate,
)

__all__ = [
    "_build_add_keyboard",
    "_build_compare_keyboard",
    "_escape",
    "_format_cart",
    "_format_cart_plaintext",
    "_format_compare",
    "_format_matched_compare",
    "_format_offer_line",
    "_format_price",
    "_format_search_results",
    "_humanize_error",
    "_pick_reference_and_matches",
    "_reduce_to_cheapest",
    "_synthesize_matched_results",
    "_truncate",
    "build_dispatcher",
    "router",
]
