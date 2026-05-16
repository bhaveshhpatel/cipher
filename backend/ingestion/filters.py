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

GATE-001 (2026-05-16): ETF noise exclusion — hardcoded pre-tier gate.
    is_etf_noise_symbol() is the authoritative FIRST gate in _process_trade().
    It fires before ANY tier lookup, before gate_config_store, and before
    parse_tradier_trade(). It is HARDCODED — it does NOT depend on:
      - gate_config_store.get("exclude_indices", ...)
      - the OCC registry / symbol_tier
      - any DB read or hot-reload
    This ensures index ETF tickers can never leak through due to a stale
    config epoch, a hot-reload failure, or a DB outage.

    _ETF_NOISE_BLOCKLIST mirrors _INDEX_SYMBOLS in services/tradier_stream.py.
    When adding to one, add to both.
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

# GATE-001: Hardcoded ETF noise blocklist.
# Mirrors _INDEX_SYMBOLS in services/tradier_stream.py — keep in sync.
# These are high-volume ETF options whose flow obscures single-stock signals.
# Categorised for easy auditing:
#   Broad-market index ETFs  : SPY, QQQ, IWM, DIA
#   Volatility products      : VXX, UVXY, SVXY
#   Commodity / bond ETFs    : GLD, SLV, TLT, HYG, EEM
#   Leveraged equity ETFs    : TQQQ, SOXL, SOXS, TECS, TECL
#   Thematic (ARK)           : ARKK, ARKQ, ARKW, ARKG, ARKX
#   High-volume sector ETFs  : XLF, XLE, XLK, XBI, IBB, IBIT, GDX, GDXJ
_ETF_NOISE_BLOCKLIST: frozenset[str] = frozenset({
    # Broad-market index ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Volatility products
    "VXX", "UVXY", "SVXY",
    # Commodity / bond ETFs
    "GLD", "SLV", "TLT", "HYG", "EEM",
    # Leveraged equity ETFs
    "TQQQ", "SOXL", "SOXS", "TECS", "TECL",
    # Thematic (ARK)
    "ARKK", "ARKQ", "ARKW", "ARKG", "ARKX",
    # High-volume sector ETFs
    "XLF", "XLE", "XLK", "XBI", "IBB", "IBIT", "GDX", "GDXJ",
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


def is_etf_noise_symbol(symbol: str) -> bool:
    """Return True if symbol is a high-volume ETF whose options must be excluded.

    GATE-001: This is the HARDCODED first gate in _process_trade().
    It fires before any tier lookup, before gate_config_store, and before
    parse_tradier_trade(). It does NOT depend on any config or DB state.

    Use this in conjunction with the config-driven _resolve_exclude_indices()
    check in tradier_stream._process_trade() for defense-in-depth. This
    function is the hard backstop that cannot be disabled by config drift.

    Args:
        symbol: Raw ticker string (already extracted from OCC symbol, uppercase).
                Must be pre-stripped — e.g. the result of raw.get("symbol",
                "").split(" ")[0].upper().

    Returns:
        True  → symbol is an ETF noise ticker; caller must drop the tick.
        False → symbol is not in the ETF noise blocklist; proceed normally.
    """
    if not symbol:
        return False
    return symbol.strip().upper() in _ETF_NOISE_BLOCKLIST
