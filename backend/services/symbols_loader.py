"""
services/symbols_loader.py

Fetches and validates the full options-tradeable universe, then computes
stream eligibility via Tradier batch quotes (Step 3).

Universe pipeline:
  Step 1 — CBOE CSV → ~5,500 raw symbols
  Step 2 — Tradier /expirations validation → ~5,500 confirmed optionable symbols
  Step 3 — Tradier /v1/markets/quotes batch fetch (200/request, ~28 parallel)
             → last_price, volume per symbol
             → compute stream_eligible flag
  Step 4 — Extract stream_eligible=true symbols → StreamPoolManager (~1,000–2,000)
  Step 5 — Save snapshot to options_universe_snapshots

Fallback priority:
  1. CBOE + Tradier validated + batch quotes (full pipeline)
  2. DB snapshot if provided (stream_eligible_set = None, quotes = [])
  3. SEED_SYMBOLS as last resort (stream_eligible_set = None, quotes = [])

Returns (symbols, source, stream_eligible_set, quotes) from load_universe().
  - quotes is always a list[SymbolQuote] (empty list on fallback paths)
  - upsert_symbol_quotes() is NOT called here — caller (main.py) does it
    AFTER save_snapshot() so the active snapshot row already exists.
"""
import asyncio
import csv
import io
import logging
from dataclasses import dataclass
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
_QUOTES_URL           = f"{settings.TRADIER_BASE_URL}/v1/markets/quotes"
_CONNECT_TIMEOUT      = 20.0
_VALIDATE_CONCURRENCY = 20
_VALIDATE_TIMEOUT     = 8.0
_QUOTES_TIMEOUT       = 12.0


# ---------------------------------------------------------------------------
# Quote result dataclass (Step 3 output per symbol)
# ---------------------------------------------------------------------------

@dataclass
class SymbolQuote:
    symbol: str
    last_price: Optional[float] = None
    volume: Optional[int] = None
    stream_eligible: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def load_universe(
    *,
    db_snapshot: Optional[list[str]] = None,
) -> tuple[list[str], str, Optional[set[str]], list["SymbolQuote"]]:
    """
    Return (symbols, source, stream_eligible_set, quotes) where:
      symbols             — full validated universe list
      source              — 'tradier_validated' | 'cache' | 'seed_fallback'
      stream_eligible_set — set of symbols eligible for streaming,
                            or None when source is cache/seed (pipeline not run)
      quotes              — list[SymbolQuote] with last_price/volume/stream_eligible
                            per symbol; empty list on fallback paths

    NOTE: upsert_symbol_quotes() is NOT called here.
    The caller (main.py) must call it AFTER save_snapshot() so that the
    active snapshot row already exists when the upsert runs.
    """
    try:
        symbols = await _fetch_and_validate()
        if symbols:
            log.info("Universe loaded: %d symbols (CBOE + Tradier validated)", len(symbols))

            # Step 3 — batch quotes → stream eligibility
            quotes = await _fetch_batch_quotes(symbols)
            eligible_set = {q.symbol for q in quotes if q.stream_eligible}

            # Priority symbols are always stream-eligible regardless of price/volume
            priority = set(settings.priority_symbols)
            eligible_set |= (priority & set(symbols))

            log.info(
                "Step 3 complete: %d / %d symbols stream-eligible (%.1f%%) "
                "[min_price=%.2f, min_volume=%d]",
                len(eligible_set), len(symbols),
                100 * len(eligible_set) / len(symbols) if symbols else 0,
                settings.UNIVERSE_MIN_PRICE,
                settings.UNIVERSE_MIN_VOLUME,
            )

            return symbols, "tradier_validated", eligible_set, quotes

        log.warning("CBOE+Tradier universe fetch returned 0 valid symbols — using fallback")
    except Exception as e:
        log.error("Universe fetch failed unexpectedly: %s — using fallback", e)

    syms, source = _fallback(db_snapshot)
    return syms, source, None, []


def _fallback(db_snapshot: Optional[list[str]]) -> tuple[list[str], str]:
    if db_snapshot:
        log.info("Using DB snapshot as fallback: %d symbols", len(db_snapshot))
        return db_snapshot, "cache"
    log.warning("No DB snapshot — using seed fallback (%d symbols)", len(SEED_SYMBOLS))
    return list(SEED_SYMBOLS), "seed_fallback"


# ---------------------------------------------------------------------------
# Step 1: CBOE fetch
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
# Step 2: Tradier per-symbol expiration validation
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


# ---------------------------------------------------------------------------
# Step 3: Tradier batch quotes → stream eligibility
# ---------------------------------------------------------------------------

async def _fetch_batch_quotes(symbols: list[str]) -> list[SymbolQuote]:
    """
    Fetch last_price and volume for all symbols via Tradier /v1/markets/quotes.

    Batches symbols into groups of UNIVERSE_QUOTES_BATCH_SIZE (default 200),
    fires up to UNIVERSE_QUOTES_CONCURRENCY (default 28) batches in parallel.

    stream_eligible = last_price >= UNIVERSE_MIN_PRICE
                      AND volume  >= UNIVERSE_MIN_VOLUME

    Any symbol that errors or returns no quote data gets:
      last_price=None, volume=None, stream_eligible=False
    (unless it is in priority_symbols, which is forced eligible by the caller).
    """
    if not symbols:
        return []

    batch_size  = settings.UNIVERSE_QUOTES_BATCH_SIZE
    concurrency = settings.UNIVERSE_QUOTES_CONCURRENCY
    min_price   = settings.UNIVERSE_MIN_PRICE
    min_volume  = settings.UNIVERSE_MIN_VOLUME
    headers     = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept":        "application/json",
    }

    batches: list[list[str]] = [
        symbols[i : i + batch_size]
        for i in range(0, len(symbols), batch_size)
    ]
    log.info(
        "Step 3: fetching quotes for %d symbols in %d batches (%d symbols/batch, %d concurrent)",
        len(symbols), len(batches), batch_size, concurrency,
    )

    sem = asyncio.Semaphore(concurrency)

    async def _fetch_batch(batch: list[str]) -> list[SymbolQuote]:
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=_QUOTES_TIMEOUT) as client:
                    resp = await client.get(
                        _QUOTES_URL,
                        headers=headers,
                        params={
                            "symbols": ",".join(batch),
                            "greeks": "false",
                        },
                    )

                if resp.status_code != 200:
                    log.warning(
                        "Quotes batch HTTP %d for %d symbols (first: %s)",
                        resp.status_code, len(batch), batch[0],
                    )
                    return [SymbolQuote(symbol=s) for s in batch]

                data       = resp.json()
                quotes_raw = data.get("quotes", {}).get("quote") or []

                # Tradier returns a dict (not list) when only 1 symbol in the batch
                if isinstance(quotes_raw, dict):
                    quotes_raw = [quotes_raw]

                quote_map: dict[str, dict] = {
                    q["symbol"]: q for q in quotes_raw if "symbol" in q
                }

                results: list[SymbolQuote] = []
                for symbol in batch:
                    raw = quote_map.get(symbol)
                    if raw is None:
                        results.append(SymbolQuote(symbol=symbol))
                        continue

                    last_price: Optional[float] = None
                    volume:     Optional[int]   = None

                    for price_key in ("last", "last_price", "close", "prevclose"):
                        val = raw.get(price_key)
                        if val is not None:
                            try:
                                last_price = float(val)
                                break
                            except (TypeError, ValueError):
                                pass

                    # Always use average_volume (30-day avg daily stock volume) for eligibility.
                    # Today's intraday "volume" is 0 pre-market and irrelevant for universe filtering.
                    avg_vol_val = raw.get("average_volume")
                    if avg_vol_val is not None:
                        try:
                            volume = int(float(avg_vol_val))
                        except (TypeError, ValueError):
                            pass
                    # If today's volume is 0 (pre-market) fall back to 30-day average volume
                    if not volume and avg_vol_val is not None:
                        try:
                            volume = int(float(avg_vol_val))
                        except (TypeError, ValueError):
                            pass

                    eligible = (
                        last_price is not None
                        and volume  is not None
                        and last_price >= min_price
                        and volume    >= min_volume
                    )

                    results.append(SymbolQuote(
                        symbol=symbol,
                        last_price=last_price,
                        volume=volume,
                        stream_eligible=eligible,
                    ))

                return results

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                log.warning("Quotes batch network error (first: %s): %s", batch[0], e)
                return [SymbolQuote(symbol=s) for s in batch]
            except Exception as e:
                log.error("Quotes batch unexpected error (first: %s): %s", batch[0], e)
                return [SymbolQuote(symbol=s) for s in batch]

    batch_results = await asyncio.gather(*[_fetch_batch(b) for b in batches])

    all_quotes: list[SymbolQuote] = []
    for batch_result in batch_results:
        all_quotes.extend(batch_result)

    log.info(
        "Quotes fetch complete: %d/%d symbols have price+volume data, %d stream-eligible",
        sum(1 for q in all_quotes if q.last_price is not None),
        len(all_quotes),
        sum(1 for q in all_quotes if q.stream_eligible),
    )
    return all_quotes
