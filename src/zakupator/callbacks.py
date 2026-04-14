"""Typed callback_data schemas for the bot's inline keyboards.

Telegram imposes a hard 64-byte limit on `callback_data` strings and gives
us nothing else structural — it's opaque bytes on the wire. We used to just
f-string them together (`f"a:{token}:{idx}"`) and parse on the other side,
which worked but had two problems:

1. No type checking. A typo in the format ("a:" vs "add:") or a field-order
   swap would only surface at runtime when the user clicked the button.
2. Handler filters like `lambda c: c.data and c.data.startswith("a:")` are
   invisible to mypy and brittle if the prefix changes.

aiogram 3 ships a `CallbackData` factory that solves both: declare the
schema once, call `.pack()` when building the keyboard and `.filter()` when
registering the handler. Prefixes become compile-time constants the router
hands us already parsed.

We still pick very short prefixes and field names because the 64-byte
budget is tight — a /search button carries a 6-char token plus a small
integer index, and that leaves room for little else. The history flow
embeds the whole query string, so we truncate aggressively there.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

# Hard Telegram limit. aiogram validates on pack() so going over raises —
# we surface this as a named constant so callers can pre-trim strings
# (notably the /history query) before calling .pack().
CALLBACK_DATA_MAX_BYTES = 64


class AddToCart(CallbackData, prefix="a"):
    """Add a displayed search offer to the user's cart.

    `token` keys into `SearchCache`; `idx` is the flat index of the offer
    within that cached result list. The token is short (6 chars) and the
    index is tiny, so this fits comfortably under the 64-byte limit even
    for big result sets.
    """

    token: str
    idx: int


class ChangeQty(CallbackData, prefix="q"):
    """±/info buttons on an existing cart line.

    `op` is one of "+", "-", "?". The "?" variant is a no-op info button —
    the middle label showing the current quantity, which we want to be
    tappable so users get a toast if they hit it by accident.
    """

    op: str
    item_id: int


class RemoveItem(CallbackData, prefix="r"):
    """Trash icon on a cart line — delete regardless of current quantity."""

    item_id: int


class ClearCart(CallbackData, prefix="c"):
    """Clear-cart flow. Three actions reuse one prefix:

    - "ask": user tapped 🧹 inside /cart — show confirm prompt.
    - "yes": confirm — wipe the cart.
    - "no":  cancel the prompt.
    """

    action: str


class CopyCart(CallbackData, prefix="cp"):
    """Dump cart as plain text for easy copy/paste.

    Only one action today ("list") but the field keeps room to grow
    (e.g. "list_by_service") without needing a new prefix.
    """

    action: str


class HistoryPick(CallbackData, prefix="h"):
    """Re-run a previous search query from the /history list.

    Query text is embedded directly. aiogram's `_unpack_from_str` splits
    with maxsplit == field_count, so a ":" inside the query (rare but
    possible) is preserved on unpack. Callers must still keep the *packed*
    string under `CALLBACK_DATA_MAX_BYTES` — see `pack_history_pick`.
    """

    query: str


def pack_history_pick(query: str) -> str:
    """Pack a HistoryPick callback, trimming the query to fit 64 bytes.

    Historical note: we used to format `f"h:{q}"[:63]` inline at the
    keyboard-build site. That worked but the trimming logic was silently
    tied to the prefix length. Keeping it here next to the schema makes
    the coupling explicit.
    """
    packed = HistoryPick(query=query).pack()
    if len(packed.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES:
        return packed
    # Over budget — shorten the query and try again. Byte-wise trimming is
    # important because Cyrillic is 2 bytes per char in UTF-8.
    encoded = query.encode("utf-8")
    # Reserve 2 bytes for "h:" prefix + separator.
    budget = CALLBACK_DATA_MAX_BYTES - 2
    while len(encoded) > budget:
        query = query[:-1]
        encoded = query.encode("utf-8")
    return HistoryPick(query=query).pack()
