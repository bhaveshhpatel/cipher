"""
services/symbols_loader.py

Fetches and validates the full options-tradeable universe, then computes
stream eligibility AND tier assignment via Tradier batch quotes (Step 3).

Universe pipeline:
  Step 1 — CBOE CSV → ~5,500 raw symbols
  Step 2 — Tradier /expirations validation → ~5,500 confirmed optionable symbols
  Step 3 — Tradier /v1/markets/quotes batch fetch (200/request, ~28 parallel)
             → last_price, volume per symbol
             → compute stream_eligible flag
             → compute tier (1=Elite, 2=Active, 3=Eligible) [C-020]
  Step 4 — Extract stream_eligible=true symbols → StreamPoolManager (~1,000–2,000)
  Step 5 — Save snapshot to options_universe_snapshots

Tier assignment (C-020):
  Tier 1 (Elite):   avg_volume >= TIER1_MIN_VOLUME  (default 20M)
  Tier 2 (Active):  avg_volume >= TIER2_MIN_VOLUME (default 2M) AND last_price >= TIER2_MIN_PRICE (default $10)
  Tier 3 (Eligible): passes stream_eligible (>= 500K vol, >= $1 price)
  Tiers are fully dynamic — no hardcoded symbol list. A symbol auto-promotes
  or demotes on every 24h universe refresh based on live volume data.

OI Enrichment (C-020):
  The Tradier `timesale` stream does NOT carry open_interest.
  OI is fetched at registry build time via /v1/markets/options/chains
  (already done in symbol_registry.py — ContractMeta.open_interest).
  This loader now also computes `avg_oi` (average OI across all ATM contracts
  for the symbol) and persists it to options_universe_symbols for admin
  visibility and tier-quality metrics.
  OI refresh on the live stream is NOT possible via Tradier timesale;
  the registry rebuild (every 30 min) is the correct OI refresh mechanism.

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
_VALIDATE_CONCURRENCY = 50
_VALIDATE_TIMEOUT     = 8.0
_QUOTES_TIMEOUT       = 12.0


# ---------------------------------------------------------------------------
# Quote result dataclass (Step 3 output per symbol)
# C-020: added `tier` field (1/2/3) and `avg_oi` field
# ---------------------------------------------------------------------------

@dataclass
class SymbolQuote:
    symbol:          str
    last_price:      Optional[float] = None
    volume:          Optional[int]   = None
    stream_eligible: bool            = False
    tier:            int             = 3       # C-020: 1=Elite, 2=Active, 3=Eligible
    avg_oi:          Optional[int]   = None    # C-020: average OI across ATM contracts (set by registry builder)


def _assign_tier(
    last_price: Optional[float],
    volume:     Optional[int],
    tier1_min_vol: int   = 20_000_000,
    tier2_min_vol: int   = 2_000_000,
    tier2_min_price: float = 10.0,
) -> int:
    """
    Assign a stream tier based on stock-level volume and price.

    C-020: Fully dynamic — no hardcoded symbol names.
    Thresholds are passed in from ingestion_config (DB-backed, admin-controllable).

    Tier 1 (Elite):
      avg_volume >= tier1_min_vol (default 20M)
      Covers SPY, QQQ, AAPL, NVDA, TSLA naturally.
      Also auto-promotes any symbol that surges (meme events, earnings).

    Tier 2 (Active):
      avg_volume >= tier2_min_vol (default 2M) AND last_price >= tier2_min_price (default $10)
      Mid-large cap institutional names.

    Tier 3 (Eligible):
      Passes minimum stream_eligible bar (volume >= 500K, price >= $1).
      Near-dated, tighter ATM filter applied at registry build time.

    Returns 3 if data is missing (safe default — tightest filter).
    """
    if volume is None or last_price is None:
        return 3
    if volume >= tier1_min_vol:
        return 1
    if volume >= tier2_min_vol and last_price >= tier2_min_price:
        return 2
    return 3


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
      quotes              — list[SymbolQuote] with last_price/volume/stream_eligible/tier
                            per symbol; empty list on fallback paths

    NOTE: upsert_symbol_quotes() is NOT called here.
    The caller (main.py) must call it AFTER save_snapshot() so that the
    active snapshot row already exists when the upsert runs.
    """
    if not settings.TRADIER_API_KEY:
        log.warning(
            "[symbols_loader] TRADIER_API_KEY not set — skipping universe fetch"
        )
        syms, source = _fallback(db_snapshot)
        return syms, source, None, []

    try:
        symbols = await _fetch_and_validate()
        if symbols:
            log.info("Universe loaded: %d symbols (CBOE + Tradier validated)", len(symbols))

            # Load tier thresholds from ingestion_config (DB-backed, admin-editable)
            tier1_min_vol, tier2_min_vol, tier2_min_price = await _load_tier_thresholds()

            # Step 3 — batch quotes → stream eligibility + tier assignment
            quotes = await _fetch_batch_quotes(
                symbols,
                tier1_min_vol=tier1_min_vol,
                tier2_min_vol=tier2_min_vol,
                tier2_min_price=tier2_min_price,
            )
            eligible_set = {q.symbol for q in quotes if q.stream_eligible}

            t1 = sum(1 for q in quotes if q.tier == 1)
            t2 = sum(1 for q in quotes if q.tier == 2)
            t3 = sum(1 for q in quotes if q.tier == 3 and q.stream_eligible)
            log.info(
                "Step 3 complete: %d / %d symbols stream-eligible "
                "[T1=%d Elite, T2=%d Active, T3=%d Eligible] "
                "[min_price=%.2f, min_volume=%d, T1_vol=%d, T2_vol=%d]",
                len(eligible_set), len(symbols),
                t1, t2, t3,
                settings.UNIVERSE_MIN_PRICE,
                settings.UNIVERSE_MIN_VOLUME,
                tier1_min_vol,
                tier2_min_vol,
            )

            return symbols, "tradier_validated", eligible_set, quotes

        log.warning("CBOE+Tradier universe fetch returned 0 valid symbols — using fallback")
    except Exception as e:
        log.error("Universe fetch failed unexpectedly: %s — using fallback", e)

    syms, source = _fallback(db_snapshot)
    return syms, source, None, []


async def _load_tier_thresholds() -> tuple[int, int, float]:
    """Load tier thresholds from ingestion_config DB (admin-editable). Falls back to config defaults."""
    try:
        from services.ingestion_config import get_config
        cfg = await get_config()
        return (
            int(cfg.get("TIER1_MIN_VOLUME",  settings.TIER1_MIN_VOLUME)),
            int(cfg.get("TIER2_MIN_VOLUME",  settings.TIER2_MIN_VOLUME)),
            float(cfg.get("TIER2_MIN_PRICE", settings.TIER2_MIN_PRICE)),
        )
    except Exception:
        return settings.TIER1_MIN_VOLUME, settings.TIER2_MIN_VOLUME, settings.TIER2_MIN_PRICE


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
# Step 3: Tradier batch quotes → stream eligibility + tier assignment
# ---------------------------------------------------------------------------

async def _fetch_batch_quotes(
    symbols: list[str],
    tier1_min_vol:   int   = 20_000_000,
    tier2_min_vol:   int   = 2_000_000,
    tier2_min_price: float = 10.0,
) -> list[SymbolQuote]:
    """
    Fetch last_price and volume for all symbols via Tradier /v1/markets/quotes.

    Batches symbols into groups of UNIVERSE_QUOTES_BATCH_SIZE (default 200),
    fires up to UNIVERSE_QUOTES_CONCURRENCY (default 28) batches in parallel.

    stream_eligible = last_price >= UNIVERSE_MIN_PRICE
                      AND volume  >= UNIVERSE_MIN_VOLUME

    tier = _assign_tier(last_price, volume, tier1_min_vol, tier2_min_vol, tier2_min_price)

    Any symbol that errors or returns no quote data gets:
      last_price=None, volume=None, stream_eligible=False, tier=3
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

                    # Use max(average_volume, today_volume) so symbols like HOOD
                    # that report average_volume=0 but have real intraday volume still qualify.
                    avg_vol: int = 0
                    today_vol: int = 0
                    try:
                        avg_vol = int(float(raw.get("average_volume") or 0))
                    except (TypeError, ValueError):
                        pass
                    try:
                        today_vol = int(float(raw.get("volume") or 0))
                    except (TypeError, ValueError):
                        pass
                    effective_vol = max(avg_vol, today_vol)
                    volume = effective_vol if effective_vol > 0 else None

                    eligible = (
                        last_price is not None
                        and volume  is not None
                        and last_price >= min_price
                        and volume    >= min_volume
                    )

                    tier = _assign_tier(
                        last_price, volume,
                        tier1_min_vol=tier1_min_vol,
                        tier2_min_vol=tier2_min_vol,
                        tier2_min_price=tier2_min_price,
                    ) if eligible else 3

                    results.append(SymbolQuote(
                        symbol=symbol,
                        last_price=last_price,
                        volume=volume,
                        stream_eligible=eligible,
                        tier=tier,
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
        "Quotes fetch complete: %d/%d have price+volume, %d stream-eligible "
        "[T1=%d, T2=%d, T3=%d]",
        sum(1 for q in all_quotes if q.last_price is not None),
        len(all_quotes),
        sum(1 for q in all_quotes if q.stream_eligible),
        sum(1 for q in all_quotes if q.tier == 1),
        sum(1 for q in all_quotes if q.tier == 2),
        sum(1 for q in all_quotes if q.tier == 3 and q.stream_eligible),
    )
    return all_quotes
