"""Internal tuning knobs — single source of truth for "magic numbers".

This module holds values that are *not* user-configurable at runtime:
matching thresholds, cache sizes, retry budgets, default timeouts.
They're here so a reviewer can find them without spelunking and so
tests can import them instead of reasserting literals.

User-facing configuration (bot token, DB URL, log level, default
address) lives in `config.py`, which reads from the environment.

Changing anything in this file is an intentional behavioral change —
ship with a CHANGELOG entry.
"""

from __future__ import annotations

from typing import Final

# ---- SearchEngine --------------------------------------------------------

#: Default number of offers fetched per service for a single /search query.
#: 3 fits comfortably in one Telegram message; more clutters the reply.
SEARCH_LIMIT_PER_SERVICE: Final[int] = 3

#: Wall-clock deadline for a single fan-out search across all adapters.
#: Slow services are cancelled and surfaced as `error="timeout"` so the
#: user's reply isn't held hostage by the slowest backend.
SEARCH_TIMEOUT_SECONDS: Final[float] = 12.0

# ---- Response cache (search results) -------------------------------------

#: Maximum number of cached SearchResults across all (service, query) keys.
#: Bounded so the cache can't grow without limit on a busy bot.
RESPONSE_CACHE_MAX_SIZE: Final[int] = 256

#: How long a cached SearchResult stays fresh. 5 minutes is short enough
#: that prices stay accurate yet long enough to absorb repeated clicks
#: through cart buttons without hammering upstream.
RESPONSE_CACHE_TTL_SECONDS: Final[float] = 300.0

# ---- Search (UI) cache ---------------------------------------------------

#: Max per-display search snapshots kept for callback-button resolution.
SEARCH_CACHE_MAX_SIZE: Final[int] = 512

#: How long a snapshot stays valid. Longer than the response cache because
#: users sometimes come back to an older message and click "add to cart".
SEARCH_CACHE_TTL_SECONDS: Final[float] = 30 * 60

#: Length of the random callback token. 6 chars of `[a-z0-9]` gives ~2×10⁹
#: addressable entries — collisions are a non-issue at our cache size.
SEARCH_CACHE_TOKEN_LENGTH: Final[int] = 6

# ---- Cross-service matching ----------------------------------------------

#: Minimum rapidfuzz.token_set_ratio score for two names to be considered
#: "the same product". Picked by eyeballing captured real data; sub-70 is
#: almost always wrong, 80+ strongly correlates with "yes".
MATCH_NAME_SIMILARITY_THRESHOLD: Final[int] = 80

#: Maximum relative quantity difference (min/max ratio ≥ 1 − this).
#: 0.12 allows e.g. 930ml ↔ 970ml but rejects 1L ↔ 2L.
MATCH_QUANTITY_RELATIVE_TOLERANCE: Final[float] = 0.12

# ---- Retry policy --------------------------------------------------------

#: Max retry attempts for a single HTTP call.
RETRY_MAX_ATTEMPTS: Final[int] = 3

#: Sleep durations between retries (seconds). len == max_attempts - 1.
RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.3, 1.0)

# ---- Bot UI --------------------------------------------------------------

#: Default number of recent unique search history entries in /history.
HISTORY_LIMIT: Final[int] = 10

#: Max title length rendered in an inline cart row. Keeps each row under
#: Telegram's practical width so quantity controls don't wrap.
CART_TITLE_TRUNCATE: Final[int] = 55
