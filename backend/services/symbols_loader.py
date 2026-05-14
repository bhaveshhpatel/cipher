"""
services/symbols_loader.py

Fetches and validates the full options-tradeable universe, then computes
stream eligibility via Tradier batch quotes (Step 3).

Universe pipeline:
  Step 1 — CBOE CSV → ~5,500 raw symbols
             → EXCLUDED_SYMBOLS filter applied (configurable via ingestion_config)
  Step 2 — [DISABLED] Tradier /expirations validation — commented out;
             fired 5,267 serial API calls on cold start, hitting Tradier
             rate/timeout limits → 0 valid symbols → seed fallback every
             restart.  Step 3 batch quotes already screens non-tradeable
             symbols via min_price / min_volume.  Re-enable when parallel
             schema work (Option A/B) provides a cached validation layer.
             TODO(parallel-schema): reinstate _validate_symbols() call
  Step 3 — Tradier /v1/markets/quotes batch fetch (200/request, ~28 parallel)
             → last_price, volume, average_volume, open_interest per symbol
             → compute stream_eligible flag
  Step 4 — Extract stream_eligible=true symbols → StreamPoolManager (~1,000–2,000)
  Step 5 — Save snapshot to options_universe_snapshots

Fallback priority:
  1. CBOE batch quotes (full pipeline, no per-symbol expiration check)
  2. DB snapshot if provided (stream_eligible_set = None, quotes = [])
  3. SEED_SYMBOLS as last resort (stream_eligible_set = None, quotes = [])

Returns (symbols, source, stream_eligible_set) from load_universe().
  - Callers that need quotes call _fetch_batch_quotes() directly.
  - upsert_symbol_quotes() is NOT called here — caller (main.py) does it
    AFTER save_snapshot() so the active snapshot row already exists.

Note on priority_symbols:
  After the pipeline completes, settings.priority_symbols are force-added
  to the eligible_set regardless of their price/volume thresholds.

Note on excluded_symbols:
  EXCLUDED_SYMBOLS is read live from ingestion_config (key='EXCLUDED_SYMBOLS',
  value_type='json_list', comma-separated string) via get_config() each time
  _fetch_and_validate() runs.  The 60-second TTL cache in ingestion_config
  means an admin PATCH takes effect within 60 s — no code deploy required.

  Fallback chain:
    1. ingestion_config DB key EXCLUDED_SYMBOLS (non-empty comma-separated string)
    2. settings.EXCLUDED_SYMBOLS env-var (EXCLUDED_SYMBOLS=SPY,QQQ in .env)
    3. _DEFAULT_EXCLUDED built-in frozenset (hardcoded ~40 ETF tickers)

  The filter is applied immediately after the CBOE CSV parse (Step 1),
  before any Tradier API calls — excluded symbols cost zero API quota.
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

# Default excluded symbols used when EXCLUDED_SYMBOLS is not set in
# ingestion_config (DB key empty) and not set via env-var.
# Covers: broad-market index ETFs (SPY/QQQ/IWM/DIA),
# commodity ETFs (GLD/SLV/USO), bond ETFs (TLT/HYG/LQD), international
# ETFs (EEM/EFA), sector ETFs (XL*), gold miner ETFs (GDX/GDXJ),
# leveraged/inverse ETFs (TQQQ/SQQQ/SOXL/SOXS/SPXL/SPXS), volatility
# ETPs (UVXY/SVXY/VIXY), and crypto ETFs (BITO/IBIT/ETHA).
# These instruments either lack meaningful options sweep signals or produce
# predominantly macro/hedging flow rather than the single-stock sweep
# patterns the Steamroom strategy targets.
_DEFAULT_EXCLUDED: frozenset[str] = frozenset([
    # Broad-market index ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Commodity ETFs
    "GLD", "SLV", "USO",
    # Bond ETFs
    "TLT", "HYG", "LQD", "BND", "AGG",
    # International ETFs
    "EEM", "EFA", "EWZ", "FXI", "EWY",
    # Sector ETFs
    "XLF", "XLE", "XLU", "XLK", "XLV", "XLI", "XLB", "XLP", "XLRE",
    # Gold miner ETFs
    "GDX", "GDXJ",
    # Leveraged / inverse ETFs
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS",
    "UPRO", "SPXU", "UDOW", "SDOW", "LABU", "LABD",
    "QID", "QLD", "PSQ", "SSO", "SDS", "SH",
    "RWM", "TNA", "TZA", "TSLL", "TSLQ",
    # Volatility ETPs
    "UVXY", "SVXY", "VIXY", "VXX",
    # Crypto ETFs
    "BITO", "IBIT", "ETHA", "FBTC", "BITB",
    # Fixed-income / dividend ETFs with low signal value
    "SCHD", "BKLN",
    # Other broad ETFs
    "SCHX", "IGV",
])

_CBOE_URL             = (
    "https://www.cboe.com/us/options/symboldir/"
    "equity_index_options/?download=csv"
)
_CHAIN_URL            = f"{settings.TRADIER_BASE_URL}/v1/markets/options/expirations"
_QUOTES_URL           = f"{settings.TRADIER_BASE_URL}/v1/markets/quotes"
_CONNECT_TIMEOUT      = 20.0
_VALIDATE_CONCURRENCY = 50   # kept for when _validate_symbols is re-enabled
_VALIDATE_TIMEOUT     = 8.0
_QUOTES_TIMEOUT       = 12.0


# ---------------------------------------------------------------------------
# Quote result dataclass (Step 3 output per symbol)
# ---------------------------------------------------------------------------

@dataclass
class SymbolQuote:
    symbol:         str
    last_price:     Optional[float] = None
    volume:         Optional[int]   = None
    average_volume: Optional[int]   = None
    open_interest:  Optional[int]   = None
    stream_eligible: bool           = False


# ---------------------------------------------------------------------------
# Excluded symbols helper
# ---------------------------------------------------------------------------

async def _load_excluded_symbols() -> frozenset[str]:
    """
    Load the EXCLUDED_SYMBOLS set, checking sources in priority order:

      1. ingestion_config DB key 'EXCLUDED_SYMBOLS' (json_list, comma-separated).
         Read via get_config() which has a 60-second TTL cache — an admin PATCH
         takes effect within one cache cycle without a restart.
      2. settings.EXCLUDED_SYMBOLS env-var (e.g. EXCLUDED_SYMBOLS=SPY,QQQ in .env).
         Useful for local dev overrides or infra-level pinning.
      3. _DEFAULT_EXCLUDED built-in frozenset (hardcoded ~40 ETF/index tickers).
         Used when both DB and env-var are absent or empty.

    Returns a frozenset of uppercase ticker strings.
    """
    # Source 1: live ingestion_config DB value (60s TTL cache)
    try:
        from services.ingestion_config import get_config
        cfg = await get_config()
        db_raw = cfg.get("EXCLUDED_SYMBOLS", "")
        if db_raw and str(db_raw).strip():
            parsed = frozenset(
                s.strip().upper() for s in str(db_raw).split(",") if s.strip()
            )
            if parsed:
                log.debug(
                    "EXCLUDED_SYMBOLS loaded from ingestion_config DB (%d symbols)",
                    len(parsed),
                )
                return parsed
    except Exception as exc:
        log.warning(
            "EXCLUDED_SYMBOLS: ingestion_config get_config() failed (%s) — "
            "falling back to env-var / built-in list",
            exc,
        )

    # Source 2: env-var / settings override
    env_raw = getattr(settings, "EXCLUDED_SYMBOLS", None)
    if env_raw and str(env_raw).strip():
        if isinstance(env_raw, (list, tuple, set, frozenset)):
            parsed = frozenset(str(s).strip().upper() for s in env_raw if str(s).strip())
        else:
            parsed = frozenset(s.strip().upper() for s in str(env_raw).split(",") if s.strip())
        if parsed:
            log.debug(
                "EXCLUDED_SYMBOLS loaded from settings/env-var (%d symbols)",
                len(parsed),
            )
            return parsed

    # Source 3: built-in default list
    log.debug(
        "EXCLUDED_SYMBOLS: using built-in default list (%d symbols)",
        len(_DEFAULT_EXCLUDED),
    )
    return _DEFAULT_EXCLUDED


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
                            or None when source is cache/seed (pipeline not run)

    NOTE: upsert_symbol_quotes() is NOT called here.
    The caller (main.py) must call _fetch_batch_quotes() separately if it
    needs the SymbolQuote list, then call upsert_symbol_quotes() after
    save_snapshot() so that the active snapshot row already exists.

    priority_symbols from settings are force-added to stream_eligible_set
    after the pipeline completes (they bypass price/volume thresholds).

    EXCLUDED_SYMBOLS from ingestion_config are removed from the CBOE universe
    before any Tradier API calls fire (zero API cost for excluded symbols).
    """
    if not getattr(settings, "TRADIER_API_KEY", None):
        log.warning("TRADIER_API_KEY not set — skipping full pipeline, using fallback")
        syms, source = _fallback(db_snapshot)
        return syms, source, None

    try:
        symbols = await _fetch_and_validate()
        if symbols:
            log.info("Universe loaded: %d symbols (CBOE, Step 2 validation disabled)", len(symbols))

            # Step 3 — batch quotes → stream eligibility
            quotes = await _fetch_batch_quotes(symbols)
            eligible_set: set[str] = {q.symbol for q in quotes if q.stream_eligible}

            # Force priority symbols into the eligible set regardless of thresholds
            priority = getattr(settings, "priority_symbols", None) or []
            if priority:
                eligible_set.update(priority)
                log.info(
                    "Priority symbols forced into eligible_set: %s",
                    ", ".join(str(s) for s in priority),
                )

            log.info(
                "Step 3 complete: %d / %d symbols stream-eligible (%.1f%%) "
                "[min_price=%.2f, min_volume=%d]",
                len(eligible_set), len(symbols),
                100 * len(eligible_set) / len(symbols) if symbols else 0,
                getattr(settings, "UNIVERSE_MIN_PRICE", 1.0),
                getattr(settings, "UNIVERSE_MIN_VOLUME", 100_000),
            )

            return symbols, "tradier_validated", eligible_set

        log.warning("CBOE universe fetch returned 0 symbols — using fallback")
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
# Step 1: CBOE fetch + excluded symbols filter
# ---------------------------------------------------------------------------

async def _fetch_and_validate() -> list[str]:
    raw_symbols = await _fetch_cboe_symbols()
    if not raw_symbols:
        return []

    # Apply EXCLUDED_SYMBOLS filter before any Tradier API calls.
    # Priority: ingestion_config DB → settings env-var → _DEFAULT_EXCLUDED.
    excluded = await _load_excluded_symbols()
    if excluded:
        before = len(raw_symbols)
        raw_symbols = [s for s in raw_symbols if s not in excluded]
        removed = before - len(raw_symbols)
        if removed:
            log.info(
                "EXCLUDED_SYMBOLS filter: removed %d symbols from CBOE universe "
                "(%d remaining). Active exclusion list has %d entries.",
                removed, len(raw_symbols), len(excluded),
            )

    log.info(
        "Fetched %d symbols from CBOE after exclusion filter — "
        "Step 2 validation DISABLED, proceeding to Step 3",
        len(raw_symbols),
    )

    # TODO(parallel-schema): reinstate _validate_symbols() call here once a
    # cached/parallel validation layer exists that won't block startup with
    # 5,000+ serial Tradier /expirations requests.
    #
    # if not settings.TRADIER_API_KEY:
    #     log.warning(
    #         "TRADIER_API_KEY not set — skipping per-symbol validation, "
    #         "using full CBOE list (%d symbols)",
    #         len(raw_symbols),
    #     )
    #     return raw_symbols
    #
    # valid = await _validate_symbols(raw_symbols)
    # log.info("Validation complete: %d / %d symbols passed", len(valid), len(raw_symbols))
    # return valid

    return raw_symbols


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
        # Skip the header row: 'Symbol', 'Company Name', ...
        next(reader, None)
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

        log.info("CBOE CSV parsed: %d unique symbols (before exclusion filter)", len(unique))
        return unique

    except (httpx.TimeoutException, httpx.ConnectError) as e:
        log.warning("CBOE symbol list network error: %s", e)
        return []
    except Exception as e:
        log.error("CBOE symbol list unexpected error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Step 2: Tradier per-symbol expiration validation
# DISABLED — see _fetch_and_validate() for rationale and TODO tag.
# Kept here verbatim so it can be reinstated without archaeology.
# ---------------------------------------------------------------------------

# async def _validate_symbols(symbols: list[str]) -> list[str]:
#     sem     = asyncio.Semaphore(_VALIDATE_CONCURRENCY)
#     headers = {
#         "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
#         "Accept": "application/json",
#     }
#
#     async def _check(symbol: str) -> Optional[str]:
#         async with sem:
#             try:
#                 async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
#                     resp = await client.get(
#                         _CHAIN_URL,
#                         headers=headers,
#                         params={"symbol": symbol},
#                     )
#                 if resp.status_code == 200:
#                     body = resp.text.strip()
#                     if not body:
#                         return None
#                     data        = resp.json()
#                     expirations = data.get("expirations") or {}
#                     dates       = expirations.get("date") or []
#                     if dates:
#                         return symbol
#                 return None
#             except Exception:
#                 return None
#
#     results = await asyncio.gather(*[_check(s) for s in symbols])
#     return [s for s in results if s is not None]


# Stub exported so existing imports don't break while function is disabled.
# Remove this stub when _validate_symbols is reinstated.
async def _validate_symbols(symbols: list[str]) -> list[str]:  # noqa: F811
    """DISABLED — see Step 2 comment block above.
    TODO(parallel-schema): remove this stub and uncomment the real implementation.
    """
    log.warning(
        "_validate_symbols() called but is currently DISABLED "
        "(returns input unchanged). TODO(parallel-schema)."
    )
    return list(symbols)


# ---------------------------------------------------------------------------
# Step 3: Tradier batch quotes → stream eligibility
# ---------------------------------------------------------------------------

async def _fetch_batch_quotes(symbols: list[str]) -> list[SymbolQuote]:
    """
    Fetch last_price, volume, average_volume for all symbols via Tradier /v1/markets/quotes.

    Batches symbols into groups of UNIVERSE_QUOTES_BATCH_SIZE (default 200),
    fires up to UNIVERSE_QUOTES_CONCURRENCY (default 28) batches in parallel.

    stream_eligible = last_price >= UNIVERSE_MIN_PRICE
                      AND volume  >= UNIVERSE_MIN_VOLUME

    Any symbol that errors or returns no quote data gets:
      last_price=None, volume=None, average_volume=None, stream_eligible=False
    """
    if not symbols:
        return []

    batch_size  = getattr(settings, "UNIVERSE_QUOTES_BATCH_SIZE", 200)
    concurrency = getattr(settings, "UNIVERSE_QUOTES_CONCURRENCY", 28)
    min_price   = getattr(settings, "UNIVERSE_MIN_PRICE", 1.0)
    min_volume  = getattr(settings, "UNIVERSE_MIN_VOLUME", 100_000)
    headers     = {
        "Authorization": f"Bearer {getattr(settings, 'TRADIER_API_KEY', '')}",
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
                    average_volume: Optional[int] = None

                    for price_key in ("last", "last_price", "close", "prevclose"):
                        val = raw.get(price_key)
                        if val is not None:
                            try:
                                last_price = float(val)
                                break
                            except (TypeError, ValueError):
                                pass

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

                    if avg_vol > 0:
                        average_volume = avg_vol

                    # Use max(average_volume, today_volume) so symbols like HOOD that
                    # report average_volume=0 but have real intraday volume still qualify.
                    effective_vol = max(avg_vol, today_vol)
                    volume = effective_vol if effective_vol > 0 else None

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
                        average_volume=average_volume,
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


# ---------------------------------------------------------------------------
# Step 5: Persist SymbolQuote rows to options_universe_symbols
#
# Called by main.py AFTER save_snapshot() so the snapshot FK already exists.
# Patchable by tests via patch("services.symbols_loader.upsert_symbol_quotes").
# ---------------------------------------------------------------------------

async def upsert_symbol_quotes(
    snapshot_id: int,
    quotes: list[SymbolQuote],
) -> bool:
    """
    Upsert SymbolQuote rows into options_universe_symbols for *snapshot_id*.

    This is a stub — the real implementation lives in main.py and is injected
    at startup.  Keeping this function here ensures test patches resolve
    correctly via patch("services.symbols_loader.upsert_symbol_quotes").

    Returns True on success, False on error.
    """
    log.debug(
        "[symbols_loader] upsert_symbol_quotes: snapshot_id=%s quotes=%d",
        snapshot_id, len(quotes),
    )
    return True
