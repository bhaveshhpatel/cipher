"""
services/symbols_loader.py

Fetches and validates the full options-tradeable universe from Tradier.

Lifecycle:
  1. Fetch all equity option symbols from Tradier GET /v1/markets/options/lookup
  2. Validate in parallel batches (check each symbol has a live option chain)
  3. Fall back to last DB snapshot if Tradier is unavailable
  4. Fall back to SEED_SYMBOLS as last resort if no DB snapshot exists

Design notes:
  - Validation uses asyncio.gather with controlled concurrency (semaphore)
  - Never blocks the stream — callers await load_universe() at startup then
    schedule background refresh every 24 h via refresh_universe_background()
  - All network failures are caught; the function always returns a list
"""
import asyncio
import logging
from typing import Optional

import httpx

from config import settings

log = logging.getLogger("symbols_loader")

# ---------------------------------------------------------------------------
# Seed fallback — used only when Tradier AND DB are both unavailable
# ---------------------------------------------------------------------------
SEED_SYMBOLS: list[str] = [
    "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META",
    "GOOGL", "AMD", "PLTR", "SOFI", "HOOD", "RIVN", "CRWD", "NET",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_LOOKUP_URL       = f"{settings.TRADIER_BASE_URL}/v1/markets/options/lookup"
_CHAIN_URL        = f"{settings.TRADIER_BASE_URL}/v1/markets/options/expirations"
_CONNECT_TIMEOUT  = 15.0
_VALIDATE_CONCURRENCY = 20     # parallel validation requests
_VALIDATE_TIMEOUT     = 8.0   # per-symbol timeout during validation


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def load_universe(
    *,
    db_snapshot: Optional[list[str]] = None,
) -> tuple[list[str], str]:
    """
    Return (symbols, source) where source is one of:
      'tradier_validated' — fetched + validated from Tradier API
      'cache'             — loaded from DB snapshot (fresh or stale)
      'seed_fallback'     — only the 16 hardcoded seed symbols

    Priority:
      1. Tradier API (fetch + validate)
      2. db_snapshot if provided (any age)
      3. SEED_SYMBOLS
    """
    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — cannot fetch universe, using fallback")
        return _fallback(db_snapshot)

    try:
        symbols = await _fetch_and_validate()
        if symbols:
            log.info("Universe loaded from Tradier: %d symbols", len(symbols))
            return symbols, "tradier_validated"
        log.warning("Tradier universe fetch returned 0 valid symbols — using fallback")
    except Exception as e:
        log.error("Universe fetch failed unexpectedly: %s — using fallback", e)

    return _fallback(db_snapshot)


def _fallback(db_snapshot: Optional[list[str]]) -> tuple[list[str], str]:
    if db_snapshot:
        log.info("Using DB snapshot as fallback: %d symbols", len(db_snapshot))
        return db_snapshot, "cache"
    log.warning("No DB snapshot — using seed fallback (%d symbols)", len(SEED_SYMBOLS))
    return list(SEED_SYMBOLS), "seed_fallback"


# ---------------------------------------------------------------------------
# Fetch from Tradier
# ---------------------------------------------------------------------------
async def _fetch_and_validate() -> list[str]:
    """
    1. GET /v1/markets/options/lookup  → raw list of optionable symbols
    2. Validate each symbol in parallel batches
    Returns only symbols that pass validation.
    """
    raw_symbols = await _fetch_optionable_symbols()
    if not raw_symbols:
        return []

    log.info("Fetched %d raw optionable symbols — starting validation", len(raw_symbols))
    valid = await _validate_symbols(raw_symbols)
    log.info("Validation complete: %d / %d symbols passed", len(valid), len(raw_symbols))
    return valid


async def _fetch_optionable_symbols() -> list[str]:
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
            resp = await client.get(_LOOKUP_URL, headers=headers)
        if resp.status_code == 401:
            log.error("Tradier 401 on options/lookup — bad API key")
            return []
        resp.raise_for_status()
        data = resp.json()
        # Response: {"symbols": [{"rootSymbol": "AAPL", ...}, ...]}
        symbols_data = data.get("symbols") or []
        if isinstance(symbols_data, dict):
            symbols_data = [symbols_data]
        symbols = [
            s.get("rootSymbol") or s.get("symbol", "")
            for s in symbols_data
            if isinstance(s, dict)
        ]
        return [s.strip().upper() for s in symbols if s and s.strip()]
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        log.warning("options/lookup network error: %s", e)
        return []
    except Exception as e:
        log.error("options/lookup unexpected error: %s", e)
        return []


async def _validate_symbols(symbols: list[str]) -> list[str]:
    """
    Validate each symbol by confirming Tradier has option expirations for it.
    Uses a semaphore to cap concurrency at _VALIDATE_CONCURRENCY.
    """
    sem = asyncio.Semaphore(_VALIDATE_CONCURRENCY)
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

    async def _check(symbol: str) -> Optional[str]:
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
                    resp = await client.get(
                        _CHAIN_URL,
                        headers=headers,
                        params={"symbol": symbol},
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    expirations = data.get("expirations") or {}
                    dates = expirations.get("date") or []
                    if dates:  # has at least one expiration
                        return symbol
                return None
            except Exception:
                return None

    results = await asyncio.gather(*[_check(s) for s in symbols])
    return [s for s in results if s is not None]
