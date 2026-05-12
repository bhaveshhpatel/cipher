"""ingestion/filters.py

Structural invariant filters applied at the ingestion boundary.

REARCH-001 (2026-05-09): Index symbol exclusion.
    Index tickers (SPX, NDX, VIX, etc.) have fundamentally different mechanics
    (cash-settled, no share-equivalent underlying, AM/PM settlement ambiguity)
    that corrupt every flow quality metric. No index ticker should ever enter
    the ingestion pipeline.

    is_index_symbol() is the authoritative gate. It is called:
      - As the first guard in tradier_stream._process_trade() (defense-in-depth)
      - Inside ingestion.config.validate_symbol() (API-layer enforcement)

    The blacklist is a module-level frozenset — O(1) lookup, survives restarts
    without a DB read, testable in pure unit tests with zero mocking.
"""

from __future__ import annotations

_INDEX_BLACKLIST: frozenset[str] = frozenset({
    "SPX",
    "SPXW",
    "SPXPM",
    "NDX",
    "NDXP",
    "VIX",
    "VIXW",
    "RUT",
    "MRUT",
    "DJX",
    "XSP",
})


def is_index_symbol(symbol: str) -> bool:
    """Return True if symbol is a cash-settled index and must be rejected at ingestion.

    Rejection criteria (OR logic — either condition triggers rejection):
      1. Dollar-prefix:  symbol starts with '$' (e.g. '$SPX', '$NDX.X')
      2. Exact blacklist: symbol (uppercased, stripped) is in _INDEX_BLACKLIST

    CRITICAL — prefix guard is '$' only, never 'SPX':
      Leveraged ETFs (SPXL, SPXS, SOXL) share a prefix with index tickers but
      are equities and must NOT be rejected. Only the '$' prefix is a reliable
      structural indicator of an index symbol in Tradier's OCC format.

    Args:
        symbol: Raw ticker string as received from Tradier or the admin API.
                May be empty, lowercase, or contain whitespace — all handled.

    Returns:
        True  → symbol is an index; caller must drop/reject it.
        False → symbol is not a known index; caller may proceed normally.
    """
    if not symbol:
        return False
    s = symbol.strip().upper()
    return s.startswith("$") or s in _INDEX_BLACKLIST
