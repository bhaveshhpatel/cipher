"""
ingestion/config.py — Ingestion pipeline configuration.

Provides symbol lists, validation helpers, and runtime knobs used by the
OCC ingestion pipeline. Mirrors the constants in services/ingestion_config.py
but is importable as `ingestion.config` for the test suite and any pipeline
module that runs outside the services/ namespace.

Public API:
  DEFAULT_SYMBOLS       list[str]   — baseline optionable universe
  SYMBOLS               list[str]   — alias for DEFAULT_SYMBOLS (mutable)
  TRADIER_API_KEY       str | None  — from env TRADIER_API_KEY
  MIN_PREMIUM           int         — minimum flow premium filter (25_000)
  INGESTION_ENABLED     bool        — master on/off flag (env INGESTION_ENABLED)
  get_symbols()         → list[str]
  add_symbol(s)         → None
  remove_symbol(s)      → None   (silent no-op if missing)
  validate_symbol(s)    → bool
  apply_config(cfg)     → None   (dict with optional 'symbols' key)

REARCH-001 (2026-05-09): validate_symbol() now rejects index tickers via
  is_index_symbol() as a second gate, after the structural alpha/length checks.
  This ensures index symbols added through the admin API or apply_config() are
  rejected at the application layer even if they slip past the streaming guard.
"""
import os
from typing import Optional

from ingestion.filters import is_index_symbol

# ---------------------------------------------------------------------------
# API key / feature flags
# ---------------------------------------------------------------------------
TRADIER_API_KEY: Optional[str] = os.environ.get("TRADIER_API_KEY")
INGESTION_ENABLED: bool = os.environ.get("INGESTION_ENABLED", "true").lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Premium threshold
# ---------------------------------------------------------------------------
MIN_PREMIUM: int = 25_000          # $25k minimum — filters noise from pipeline
PREMIUM_THRESHOLD = MIN_PREMIUM    # alias

# ---------------------------------------------------------------------------
# Default universe — top optionable equities + major ETFs
# ---------------------------------------------------------------------------
DEFAULT_SYMBOLS: list[str] = [
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG", "XLF", "XLE",
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "GOOG",
    "NFLX", "AMD", "INTC", "QCOM", "MU", "AVGO", "TXN",
    "JPM", "BAC", "GS", "MS", "WFC", "C",
    "BABA", "JD", "NIO", "RIVN", "LCID",
    "HOOD", "SOFI", "UPST", "AFRM", "COIN",
    "DIS", "NFLX", "ROKU", "SNAP", "PINS",
    "CVX", "XOM", "OXY", "SLB",
    "MRNA", "PFE", "BNTX", "JNJ",
]
# Deduplicate while preserving order
_seen: set[str] = set()
_deduped: list[str] = []
for _s in DEFAULT_SYMBOLS:
    if _s not in _seen:
        _seen.add(_s)
        _deduped.append(_s)
DEFAULT_SYMBOLS = _deduped

# Mutable working set — starts as a copy of DEFAULT_SYMBOLS.
# Can be overridden by SYMBOLS env var (comma-separated).
_env_symbols = os.environ.get("SYMBOLS", "")
if _env_symbols.strip():
    SYMBOLS: list[str] = [s.strip().upper() for s in _env_symbols.split(",") if s.strip()]
else:
    SYMBOLS = list(DEFAULT_SYMBOLS)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_symbols() -> list[str]:
    """Return the current working symbol list."""
    return list(SYMBOLS)


def add_symbol(symbol: str) -> None:
    """Add a symbol to the working list (uppercase, deduped)."""
    s = symbol.strip().upper()
    if s and s not in SYMBOLS:
        SYMBOLS.append(s)


def remove_symbol(symbol: str) -> None:
    """Remove a symbol from the working list. Silent no-op if not present."""
    s = symbol.strip().upper()
    try:
        SYMBOLS.remove(s)
    except ValueError:
        pass


def validate_symbol(symbol: str) -> bool:
    """
    Return True if *symbol* looks like a valid US equity ticker that is
    permitted in the ingestion pipeline.

    Rejection criteria (all must pass to return True):
      1. Non-empty string
      2. 1-5 characters after strip
      3. All alphabetic (A-Z) — rejects purely numeric strings
      4. NOT an index symbol (REARCH-001) — is_index_symbol() returns False

    The index check is the LAST gate so structural rejects (empty, too long,
    non-alpha) short-circuit before the frozenset lookup.

    REARCH-001 note: This is the API-layer enforcement point. The streaming
    boundary guard in _process_trade() is a separate, independent defense that
    this function must never be used to replace.
    """
    if not symbol or not isinstance(symbol, str):
        return False
    s = symbol.strip()
    if not s:
        return False
    if not s.isalpha():
        return False
    if len(s) > 5:
        return False
    # REARCH-001: index symbols pass alpha/length checks but must be rejected.
    if is_index_symbol(s):
        return False
    return True


def apply_config(cfg: dict) -> None:
    """
    Apply a configuration dict to the running ingestion config.
    Recognised keys:
      symbols  list[str]  — replaces the working SYMBOLS list
    Other keys are silently ignored (forward-compat).
    """
    if "symbols" in cfg:
        new_syms = [s.strip().upper() for s in cfg["symbols"] if isinstance(s, str) and s.strip()]
        SYMBOLS.clear()
        SYMBOLS.extend(new_syms)
