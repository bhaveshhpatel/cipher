"""
services/symbols_loader.py

Fetches and validates the full options-tradeable universe.

Universe source priority:
  1. CBOE public equity+index options symbol list (CSV, no auth required)
     https://www.cboe.com/us/options/symboldir/equity_index_options/?download=csv
  2. Falls back to last DB snapshot if CBOE fetch fails
  3. Falls back to SEED_SYMBOLS as last resort if no DB snapshot exists

Validation:
  Each symbol fetched from CBOE is validated via Tradier GET
  /v1/markets/options/expirations to confirm live option chains exist.
  Validation runs in parallel batches (semaphore-controlled concurrency).

Design notes:
  - Validation uses asyncio.gather with controlled concurrency (semaphore)
  - Never blocks the stream — callers await load_universe() at startup then
    schedule background refresh every 24 h via refresh_universe_background()
  - All network failures are caught; the function always returns a list
"""
import asyncio
import csv
import io
import logging
from typing import Optional

import httpx

from config import settings

log = logging.getLogger("symbols_loader")

# ---------------------------------------------------------------------------
# Seed fallback — used only when CBOE AND DB are both unavailable
# ---------------------------------------------------------------------------
SEED_SYMBOLS: list[str] = [
    "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META",
    "GOOGL", "AMD", "PLTR", "SOFI", "HOOD", "RIVN", "CRWD", "NET",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# CBOE public options symbol directory — no API key required
_CBOE_URL = (
    "https://www.cboe.com/us/options/symboldir/"
    "equity_index_options/?download=csv"
)
_CHAIN_URL            = f"{settings.TRADIER_BASE_URL}/v1/markets/options/expirations"
_CONNECT_TIMEOUT      = 20.0
_VALIDATE_CONCURRENCY = 20      # parallel validation requests
_VALIDATE_TIMEOUT     = 8.0     # per-symbol timeout during validation


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def load_universe(
    *,
    db_snapshot: Optional[list[str]] = None,
) -> tuple[list[str], str]:
    """
    Return (symbols, source) where source is one of:
      'tradier_validated' — fetched from CBOE + validated via Tradier API
      'cache'             — loaded from DB snapshot (fresh or stale)
      'seed_fallback'     — only the 16 hardcoded seed symbols

    Priority:
      1. CBOE fetch + Tradier validation
      2. db_snapshot if provided (any age)
      3. SEED_SYMBOLS
    """
    try:
        symbols = await _fetch_and_validate()
        if symbols:
            log.info("Universe loaded: %d symbols (CBOE + Tradier validated)", len(symbols))
            return symbols, "tradier_validated"
        log.warning("CBOE+Tradier universe fetch returned 0 valid symbols — using fallback")
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
# Fetch from CBOE
# ---------------------------------------------------------------------------
async def _fetch_and_validate() -> list[str]:
    """
    1. GET CBOE equity+index options CSV → raw list of optionable symbols
    2. Validate each symbol via Tradier expirations endpoint in parallel batches
    Returns only symbols that pass validation (have live option chains).
    """
    raw_symbols = await _fetch_cboe_symbols()
    if not raw_symbols:
        return []

    log.info("Fetched %d raw symbols from CBOE — starting Tradier validation", len(raw_symbols))

    if not settings.TRADIER_API_KEY:
        # No Tradier key — skip validation, trust CBOE list as-is
        log.warning(
            "TRADIER_API_KEY not set — skipping per-symbol validation, "
            "using full CBOE list (%d symbols)",
            len(raw_symbols),
        )
        return raw_symbols

    valid = await _validate_symbols(raw_symbols)
    log.info("Validation complete: %d / %d symbols passed", len(valid), len(raw_symbols))
    return valid


async def _fetch_cboe_symbols() -> list[str]:
    """
    Download CBOE's public equity+index options symbol directory CSV.

    CSV format (after header rows):
      "Company Name","OSI Symbol","Exchange","Tick"
      "AAPL","AAPL","C2","NOR"
      ...

    The OSI Symbol column (index 1) is the root ticker we want.
    CBOE includes a few header/comment rows before the real CSV — we skip
    any row where the second column doesn't look like a valid ticker.
    """
    try:
        async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
            resp = await client.get(
                _CBOE_URL,
                headers={"User-Agent": "cipher-backend/1.0"},
                follow_redirects=True,
            )

        if resp.status_code != 200:
            log.error(
                "CBOE symbol list returned HTTP %d — body: %s",
                resp.status_code, resp.text[:200],
            )
            return []

        content = resp.text
        if not content or not content.strip():
            log.error("CBOE symbol list response was empty")
            return []

        symbols: list[str] = []
        reader = csv.reader(io.StringIO(content))
        for row in reader:
            if len(row) < 2:
                continue
            ticker = row[1].strip().strip('"').upper()
            # Skip header rows and invalid entries
            if not ticker or not ticker.isalpha() or len(ticker) > 6:
                continue
            symbols.append(ticker)

        # Deduplicate preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique.append(s)

        log.info("CBOE CSV parsed: %d unique symbols", len(unique))
        return unique

    except (httpx.TimeoutException, httpx.ConnectError) as e:
        log.warning("CBOE symbol list network error: %s", e)
        return []
    except Exception as e:
        log.error("CBOE symbol list unexpected error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Tradier per-symbol validation
# ---------------------------------------------------------------------------
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
                    body = resp.text.strip()
                    if not body:
                        return None
                    data = resp.json()
                    expirations = data.get("expirations") or {}
                    dates = expirations.get("date") or []
                    if dates:
                        return symbol
                return None
            except Exception:
                return None

    results = await asyncio.gather(*[_check(s) for s in symbols])
    return [s for s in results if s is not None]
