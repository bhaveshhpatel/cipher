"""
services/symbols_loader.py

Fetches and validates the full options-tradeable universe.
After validation, runs universe_screener to determine stream eligibility.

Universe source priority:
  1. CBOE public equity+index options symbol list (CSV, no auth required)
  2. Falls back to last DB snapshot if CBOE fetch fails
  3. Falls back to SEED_SYMBOLS as last resort

Returns (symbols, source, stream_eligible_set) from load_universe().
"""
import asyncio
import csv
import io
import logging
from typing import Optional

import httpx

from config import settings

log = logging.getLogger("symbols_loader")

SEED_SYMBOLS: list[str] = [
    "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META",
    "GOOGL", "AMD", "PLTR", "SOFI", "HOOD", "RIVN", "CRWD", "NET",
]

_CBOE_URL             = (
    "https://www.cboe.com/us/options/symboldir/"
    "equity_index_options/?download=csv"
)
_CHAIN_URL            = f"{settings.TRADIER_BASE_URL}/v1/markets/options/expirations"
_CONNECT_TIMEOUT      = 20.0
_VALIDATE_CONCURRENCY = 20
_VALIDATE_TIMEOUT     = 8.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def load_universe(
    *,
    db_snapshot: Optional[list[str]] = None,
) -> tuple[list[str], str, Optional[set[str]]]:
    """
    Return (symbols, source, stream_eligible_set) where:
      symbols             — full validated universe list
      source              — 'tradier_validated' | 'cache' | 'seed_fallback'
      stream_eligible_set — set of symbols eligible for streaming,
                            or None when source is cache/seed (screener not run)

    Priority:
      1. CBOE fetch + Tradier validation → then screen for stream eligibility
      2. db_snapshot if provided (any age) → stream_eligible_set = None
      3. SEED_SYMBOLS                      → stream_eligible_set = None
    """
    try:
        symbols = await _fetch_and_validate()
        if symbols:
            log.info("Universe loaded: %d symbols (CBOE + Tradier validated)", len(symbols))
            # Run stream-eligibility screening
            from services.universe_screener import screen_universe
            screen_result = await screen_universe(symbols)
            log.info(
                "Universe screened: %d eligible / %d total (%.1f%%)",
                len(screen_result.eligible), len(symbols),
                100 * len(screen_result.eligible) / len(symbols) if symbols else 0,
            )
            eligible_set = set(screen_result.eligible)
            return symbols, "tradier_validated", eligible_set
        log.warning("CBOE+Tradier universe fetch returned 0 valid symbols — using fallback")
    except Exception as e:
        log.error("Universe fetch failed unexpectedly: %s — using fallback", e)

    syms, source = _fallback(db_snapshot)
    return syms, source, None


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
    raw_symbols = await _fetch_cboe_symbols()
    if not raw_symbols:
        return []

    log.info("Fetched %d raw symbols from CBOE — starting Tradier validation", len(raw_symbols))

    if not settings.TRADIER_API_KEY:
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
            if not ticker or not ticker.isalpha() or len(ticker) > 6:
                continue
            symbols.append(ticker)

        seen:   set[str]  = set()
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
    sem     = asyncio.Semaphore(_VALIDATE_CONCURRENCY)
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
                    data        = resp.json()
                    expirations = data.get("expirations") or {}
                    dates       = expirations.get("date") or []
                    if dates:
                        return symbol
                return None
            except Exception:
                return None

    results = await asyncio.gather(*[_check(s) for s in symbols])
    return [s for s in results if s is not None]
