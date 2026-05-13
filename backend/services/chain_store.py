"""
services/chain_store.py

Persists and loads the OCC symbol registry (ContractMeta rows) to/from
the options_chain_cache Supabase table (migration 012).

Public API
----------
save_chain(snapshot_id, registry_dict)  -> bool
    Batch-upsert all ContractMeta rows for the current active snapshot.
    All batches are dispatched CONCURRENTLY via asyncio.gather (C-1 fix),
    capped at _SAVE_CONCURRENCY=10 via semaphore (HOTFIX-CHAIN-CONCURRENCY).
    Called after every SymbolRegistry.build().

load_chain(snapshot_id, max_age_hours)  -> dict[str, ContractMeta] | None
    Load all rows for snapshot_id.
    Falls back to the most-recent snapshot in options_chain_cache that
    has rows AND is within max_age_hours (default 24) (C-2 fix).
    Returns None on DB error, empty dict if no fresh chains exist.

get_contract_vol_oi(occ_symbol) -> tuple[int | None, int | None]
    ING-008: Fast in-process lookup from the module-level _vol_oi_cache
    dict keyed by OCC symbol.  Returns (volume, open_interest) or
    (None, None) on cache miss.  Zero API calls on the hot path.
    The cache is populated / refreshed by start_chain_refresh_worker().

start_chain_refresh_worker(registry_fn, tradier_client, symbols_fn)
    ING-008: Background asyncio task that refreshes the intraday chain
    data (volume + OI) every _CHAIN_REFRESH_INTERVAL_S seconds for all
    stream-eligible symbols.

    API budget: one Tradier GET /markets/options/chains call per tracked
    symbol per refresh cycle.  At 300-second cadence and a 50-symbol
    universe that is ~10 calls/minute — well within Tradier's 120 req/min
    limit.  Scale note: revisit cadence when universe exceeds ~200 symbols.

    Volume reset: the cache is fully invalidated at market open
    (call invalidate_vol_oi_cache()) so yesterday's volume never bleeds
    into early-morning events.

get_epoch() -> int
    Return the current mutation epoch counter.  Incremented on every
    successful save_chain() call.  Used by assert_epoch_parity() in
    gate_config_store to detect generation skew between stores.

Design notes
------------
- Uses service_role key (bypasses RLS) for writes.
- Batched in groups of 500 rows.
- C-1: all batches run concurrently via asyncio.gather — reduces
  22k-row persist from ~5.8s to ~300ms.
- C-2: staleness guard in _find_latest_cached_snapshot prevents
  expired contracts from silently loading on warm restart.
- Non-fatal: all errors are logged as warnings; callers must not crash on
  failure — the in-memory registry is always the source of truth.

FIX P1 (2026-04-27):
  load_chain() falls back to the most-recent snapshot that has chain rows
  when the requested snapshot_id has none. Prevents the full rebuild on
  every warm restart after a new snapshot UUID is minted.

FIX D-003 (2026-04-27):
  _find_latest_cached_snapshot() now uses ORDER BY inserted_at DESC so the
  most recently written chain is always returned on fallback, rather than
  an arbitrary row from an unordered scan.

FIX C-1 (2026-04-27):
  save_chain() now dispatches all batch upserts concurrently using
  asyncio.gather instead of sequentially inside run_in_executor.

FIX C-2 (2026-04-27):
  _find_latest_cached_snapshot() now accepts max_age_hours and filters
  inserted_at >= (now - max_age_hours) before selecting. Stale snapshots
  older than 24h are ignored, forcing a fresh build() instead.

FIX EPOCH (2026-05-07):
  Added module-level _epoch counter incremented on every successful
  save_chain(). Exposed via get_epoch() for parity assertions with
  gate_config_store.assert_epoch_parity().

ING-008 (2026-05-08):
  Added `volume` column to save_chain / load_chain / _paginate_chain.
  Added get_contract_vol_oi() — O(1) dict lookup from _vol_oi_cache.
  Added start_chain_refresh_worker() — 5-min background refresh of
  intraday volume+OI for all tracked symbols via Tradier chain API.
  Added invalidate_vol_oi_cache() for market-open reset.

FIX ING-008 (2026-05-08):
  Added Awaitable to typing imports — was missing, causing NameError at
  module load time on Python 3.12.

HOTFIX-CHAIN-CONCURRENCY (2026-05-13):
  save_chain() was creating one new Supabase client per batch via
  _client() inside _upsert_batch. With 155 batches firing concurrently,
  this instantiated 155 separate connection pools simultaneously, spiking
  memory and saturating the default threadpool. On Render's starter tier
  this caused OOM restarts and health probe timeouts.

  Fix: single _client() call outside _upsert_batch, shared across all
  batches. Added asyncio.Semaphore(_SAVE_CONCURRENCY=10) to cap concurrent
  run_in_executor threads. Wall time: ~300ms -> ~1.5s (still 4x faster
  than the original sequential 5.8s). Zero impact on streaming hot path --
  save_chain is called only at startup and every 24h; get_contract_vol_oi()
  reads _vol_oi_cache fed by start_chain_refresh_worker(), a separate path.

HOTFIX-SSL-EOF (2026-05-13):
  _upsert_batch and _sync_load_chain were using the default Supabase sync
  client which uses HTTP/2 via httpcore. Supabase's load balancer closes
  idle HTTP/2 connections after a short keep-alive window. When the sync
  client tries to reuse a stale connection it raises:
    httpx.WriteError: EOF occurred in violation of protocol (_ssl.c:2393)
  This caused chain upserts to silently fail after any idle period
  (post-midnight, post-weekend, etc.).

  Fix: create_client() accepts http_options kwarg via the underlying
  httpx transport. Force HTTP/1.1 by passing http2=False to the httpx
  SyncClient transport. HTTP/1.1 reconnects per-request so stale
  connection reuse is impossible. Supabase PostgREST upserts are not
  multiplexed — no HTTP/2 throughput benefit was being realised anyway.

HOTFIX-SSL-EOF-2 (2026-05-13):
  The previous approach patched client.postgrest._client.session after
  create_client() returned. In supabase-py >=2.x the postgrest client
  is constructed lazily and the .session attribute path changed, causing
  the patch to always fail silently and log the "Could not patch" warning.

  Fix: pass http_options={"http2": False} directly inside ClientOptions
  so supabase-py/postgrest-py constructs the httpx.Client with HTTP/1.1
  from the start. No internal attribute access needed — this is the
  supported public API for disabling HTTP/2 in supabase-py >=2.3.
  The post-construction patch block and its warning log are removed.

FIX #134 (2026-05-13): gate chain_refresh worker to market hours.
  start_chain_refresh_worker() previously ran 24/7 with no market-hours
  guard and no initial startup delay. On pre-market process restarts
  (common on Render deploys) the worker immediately fired and hammered
  Tradier /v1/markets/options/chains for every symbol — returning HTTP 400
  on every call because options data is not available pre-market. At ~3s
  per symbol across 3,834 symbols, one full pre-market cycle took ~3.2h,
  meaning the worker was still churning 400s when market opened at 9:30 ET,
  delaying valid chain data and polluting logs during the most critical window.

  Fix:
    _ET, _MARKET_OPEN, _MARKET_CLOSE: timezone-aware market-hours constants.
    _is_market_hours(): returns True only Mon–Fri 09:30–16:05 ET.
    _sleep_until_market_open(): async sleep until next 9:30 AM ET weekday.
    Worker startup: if not _is_market_hours(), sleep until open before
      first fetch cycle — no pre-market 400 spam.
    Between cycles: if not _is_market_hours(), log DEBUG and skip cycle.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Awaitable, Callable, Dict, Optional, Tuple, TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from config import settings

if TYPE_CHECKING:
    from services.symbol_registry import ContractMeta

log = logging.getLogger("chain_store")

_TABLE      = "options_chain_cache"
_BATCH_SIZE = 500
_PAGE_SIZE  = 1000
_DEFAULT_MAX_AGE_HOURS = 24

# ---------------------------------------------------------------------------
# HOTFIX-CHAIN-CONCURRENCY: cap concurrent save_chain batch writes.
# ---------------------------------------------------------------------------
_SAVE_CONCURRENCY: int = 10

# ---------------------------------------------------------------------------
# ING-008: background refresh cadence for intraday vol/OI
# ---------------------------------------------------------------------------
_CHAIN_REFRESH_INTERVAL_S: int = 300  # 5 minutes

# ---------------------------------------------------------------------------
# FIX #134: market-hours constants for chain refresh gate.
# ---------------------------------------------------------------------------
_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 5)

# ---------------------------------------------------------------------------
# ING-008: in-process vol/OI cache
# ---------------------------------------------------------------------------
_vol_oi_cache: Dict[str, Dict] = {}

# ---------------------------------------------------------------------------
# Epoch counter -- incremented on every successful save_chain().
# ---------------------------------------------------------------------------
_epoch: int = 0


def get_epoch() -> int:
    """Return the current chain_store mutation epoch (incremented per save_chain success)."""
    return _epoch


def _is_market_hours() -> bool:
    """
    FIX #134: Return True only during NYSE market hours (Mon–Fri, 09:30–16:05 ET).

    Used to gate start_chain_refresh_worker() — Tradier's options chains
    endpoint returns HTTP 400 outside these hours, so there is no point
    making API calls when the market is closed.
    """
    now = datetime.now(_ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


async def _sleep_until_market_open() -> None:
    """
    FIX #134: Async-sleep until the next 9:30 AM ET weekday open.

    Called at chain refresh worker startup when the process starts
    pre-market (or on a weekend) to prevent immediately flooding Tradier
    with HTTP 400s before options data is available.

    Handles midnight rollover: if 9:30 has already passed today, targets
    the next calendar day, then skips forward past any weekend days.
    """
    now = datetime.now(_ET)
    next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= next_open:
        next_open += timedelta(days=1)
    # Skip Saturday (5) and Sunday (6)
    while next_open.weekday() >= 5:
        next_open += timedelta(days=1)
    secs = (next_open - now).total_seconds()
    log.info(
        "[chain_store] Pre-market: chain refresh sleeping %.1f min until market open (%s ET)",
        secs / 60,
        next_open.strftime("%Y-%m-%d %H:%M"),
    )
    await asyncio.sleep(secs)


def get_contract_vol_oi(occ_symbol: str) -> Tuple[Optional[int], Optional[int]]:
    """
    ING-008: O(1) in-process lookup of (volume, open_interest) for an OCC symbol.

    Returns (volume, open_interest) from the _vol_oi_cache populated by
    start_chain_refresh_worker().  Returns (None, None) on cache miss.

    Never makes a live API call -- zero latency on the hot path.
    Cache misses are expected on cold start and for symbols not yet
    refreshed in the current cycle; callers must treat None as acceptable.
    """
    entry = _vol_oi_cache.get(occ_symbol)
    if entry is None:
        return (None, None)
    return (entry.get("volume"), entry.get("open_interest"))


def invalidate_vol_oi_cache() -> None:
    """
    ING-008: Clear the vol/OI cache at market open so yesterday's intraday
    volume never bleeds into pre-market / early-morning flow events.
    Call this from the market-open boundary handler in main.py or the stream.
    """
    _vol_oi_cache.clear()
    log.info("[chain_store] vol/OI cache invalidated (market-open reset)")


async def start_chain_refresh_worker(
    get_tracked_symbols: Callable[[], list],
    fetch_chain_fn: Callable[[str], Awaitable[list]],
) -> None:
    """
    ING-008: Background asyncio task that refreshes intraday chain data
    (volume + OI per OCC symbol) for all stream-eligible tracked symbols.

    Runs every _CHAIN_REFRESH_INTERVAL_S seconds (default: 300s / 5 min)
    ONLY during market hours (Mon–Fri 09:30–16:05 ET).

    FIX #134: Two problems in the original implementation:

    Problem 1 — No initial sleep:
      The original worker called asyncio.sleep(300) first, so it waited
      5 minutes before the first cycle. But on a pre-market deploy, that
      5-minute sleep still expired pre-market and the first fetch cycle
      would spam Tradier with 400s before options data existed.

    Problem 2 — No market-hours guard between cycles:
      After each cycle the worker slept 300s and fired again regardless of
      market hours. Outside 09:30–16:05 ET, every Tradier chain call
      returns HTTP 400. With 3,834 symbols at ~3s/symbol, one full off-hours
      cycle took ~3.2h — the worker could still be churning 400s when the
      market opened, delaying valid chain data and polluting logs.

    Fix:
      - On startup: call _sleep_until_market_open() if _is_market_hours()
        returns False. This defers the first fetch cycle until 09:30 ET,
        eliminating all pre-market 400 noise on Render restarts.
      - Between cycles: if _is_market_hours() is False, log at DEBUG and
        skip the fetch — no Tradier API calls made outside market hours.

    Parameters
    ----------
    get_tracked_symbols : Callable[[], list]
        Zero-argument callable returning current list of tracked ticker
        symbols. Called fresh each cycle so new symbols are picked up.

    fetch_chain_fn : Callable[[str], Awaitable[list]]
        Async callable accepting a ticker string, returning a list of
        contract dicts from Tradier GET /markets/options/chains.
        Must contain at minimum: "symbol" (str), "volume" (int),
        "open_interest" (int). Must return [] on error (never raise).

    API budget
    ----------
    One call per symbol per cycle. At 300s cadence, 50 symbols -> 10 req/min.
    Tradier rate limit: 120 req/min. Scale: increase interval to 600s if
    universe exceeds ~200 symbols.
    """
    log.info(
        "[chain_store] chain refresh worker started -- interval=%ds",
        _CHAIN_REFRESH_INTERVAL_S,
    )

    # FIX #134 — Problem 1: sleep until market open on startup if pre-market.
    if not _is_market_hours():
        await _sleep_until_market_open()

    try:
        while True:
            await asyncio.sleep(_CHAIN_REFRESH_INTERVAL_S)

            # FIX #134 — Problem 2: skip fetch cycles outside market hours.
            if not _is_market_hours():
                log.debug("[chain_store] Outside market hours — skipping chain refresh cycle")
                continue

            symbols = get_tracked_symbols()
            if not symbols:
                log.debug("[chain_store] chain refresh: no tracked symbols yet, skipping cycle")
                continue

            refreshed = 0
            errors = 0
            for symbol in symbols:
                try:
                    contracts = await fetch_chain_fn(symbol)
                    now_ts = datetime.now(timezone.utc).timestamp()
                    for c in contracts:
                        occ = c.get("symbol", "").strip()
                        if not occ:
                            continue
                        _vol_oi_cache[occ] = {
                            "volume":        int(c.get("volume") or 0),
                            "open_interest": int(c.get("open_interest") or 0),
                            "refreshed_at":  now_ts,
                        }
                    refreshed += 1
                except Exception as exc:
                    errors += 1
                    log.warning(
                        "[chain_store] chain refresh: error fetching %s: %s",
                        symbol, exc,
                    )

            log.info(
                "[chain_store] chain refresh cycle complete -- "
                "symbols=%d refreshed=%d errors=%d cache_size=%d",
                len(symbols), refreshed, errors, len(_vol_oi_cache),
            )

    except asyncio.CancelledError:
        log.info("[chain_store] chain refresh worker cancelled -- shutting down cleanly")
        raise


def _client() -> Client:
    """
    HOTFIX-SSL-EOF-2: force HTTP/1.1 via ClientOptions http_options.
    """
    key = settings.SUPABASE_SERVICE_KEY
    if not key:
        raise RuntimeError(
            "[chain_store] SUPABASE_SERVICE_KEY not set -- "
            "cannot read/write options_chain_cache."
        )
    options = ClientOptions(
        postgrest_client_timeout=30,
        storage_client_timeout=30,
        http_options={"http2": False},
    )
    return create_client(settings.SUPABASE_URL, key, options=options)


async def save_chain(
    snapshot_id: str,
    registry_dict: "dict[str, ContractMeta]",
) -> bool:
    """
    HOTFIX-CHAIN-CONCURRENCY + C-1: Persist all ContractMeta rows with
    bounded concurrency via a single shared Supabase client.
    """
    global _epoch

    if not registry_dict:
        log.info("[chain_store] save_chain: empty registry -- nothing to persist")
        return True

    rows = [
        {
            "snapshot_id":   snapshot_id,
            "occ_symbol":    occ,
            "ticker":        m.ticker,
            "contract_type": m.contract_type,
            "strike":        float(m.strike),
            "expiry":        m.expiry,
            "dte":           int(m.dte),
            "open_interest": int(m.open_interest),
            "volume":        int(getattr(m, "volume", 0) or 0),
            "tier":          int(m.tier),
        }
        for occ, m in registry_dict.items()
    ]
    total     = len(rows)
    batches   = [rows[i : i + _BATCH_SIZE] for i in range(0, total, _BATCH_SIZE)]
    n_batches = len(batches)

    sb  = _client()
    sem = asyncio.Semaphore(_SAVE_CONCURRENCY)

    async def _upsert_batch(batch: list, batch_num: int) -> None:
        async with sem:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: sb.table(_TABLE).upsert(
                    batch,
                    on_conflict="snapshot_id,occ_symbol",
                ).execute(),
            )
            log.info(
                "[chain_store] save_chain: batch %d/%d (%d rows)",
                batch_num, n_batches, len(batch),
            )

    try:
        await asyncio.gather(
            *[_upsert_batch(batch, i + 1) for i, batch in enumerate(batches)]
        )
        _epoch += 1
        log.info(
            "[chain_store] save_chain: persisted %d OCC contracts for snapshot %s (epoch=%d)",
            total, snapshot_id, _epoch,
        )
        return True
    except Exception as exc:
        log.warning("[chain_store] save_chain error (non-fatal): %s", exc, exc_info=True)
        return False


async def load_chain(
    snapshot_id: str,
    max_age_hours: int = _DEFAULT_MAX_AGE_HOURS,
) -> "Optional[dict[str, ContractMeta]]":
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _sync_load_chain, snapshot_id, max_age_hours
    )


def _sync_load_chain(
    snapshot_id: str,
    max_age_hours: int = _DEFAULT_MAX_AGE_HOURS,
) -> "Optional[dict[str, ContractMeta]]":
    try:
        sb = _client()

        chain = _paginate_chain(sb, snapshot_id)
        if chain:
            log.info(
                "[chain_store] load_chain: loaded %d OCC contracts from snapshot %s",
                len(chain), snapshot_id,
            )
            return chain

        log.info(
            "[chain_store] load_chain: snapshot %s has no rows -- "
            "searching for most-recent cached snapshot (max_age=%dh)",
            snapshot_id, max_age_hours,
        )
        fallback_snap = _find_latest_cached_snapshot(sb, max_age_hours=max_age_hours)
        if not fallback_snap:
            log.info(
                "[chain_store] load_chain: no cached chains within %dh -- "
                "full build() required",
                max_age_hours,
            )
            return {}

        chain = _paginate_chain(sb, fallback_snap)
        log.info(
            "[chain_store] load_chain: P1 fallback -- loaded %d OCC contracts "
            "from prior snapshot %s (active=%s)",
            len(chain), fallback_snap, snapshot_id,
        )
        return chain

    except Exception as exc:
        log.warning("[chain_store] load_chain error (non-fatal): %s", exc, exc_info=True)
        return None


def _paginate_chain(sb: Client, snapshot_id: str) -> "dict[str, 'ContractMeta']":
    from services.symbol_registry import ContractMeta
    result: list[dict] = []
    offset = 0
    while True:
        resp = (
            sb.table(_TABLE)
            .select(
                "occ_symbol,ticker,contract_type,strike,expiry,dte,open_interest,volume,tier"
            )
            .eq("snapshot_id", snapshot_id)
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        page = resp.data or []
        result.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    chain: dict = {}
    for row in result:
        occ = row.get("occ_symbol", "").strip()
        if not occ:
            continue
        cm = ContractMeta(
            ticker        = row["ticker"],
            strike        = float(row["strike"]),
            expiry        = row["expiry"],
            contract_type = row["contract_type"],
            dte           = int(row["dte"]),
            open_interest = int(row["open_interest"]),
            tier          = int(row.get("tier") or 3),
        )
        vol = row.get("volume")
        if vol is not None:
            try:
                object.__setattr__(cm, "volume", int(vol))
            except (TypeError, AttributeError):
                pass
        chain[occ] = cm
    return chain


def _find_latest_cached_snapshot(
    sb: Client,
    max_age_hours: int = _DEFAULT_MAX_AGE_HOURS,
) -> Optional[str]:
    """
    C-2 FIX: Return the snapshot_id of the most-recently inserted row in
    options_chain_cache that is within max_age_hours of now.
    """
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()
        resp = (
            sb.table(_TABLE)
            .select("snapshot_id")
            .gte("inserted_at", cutoff)
            .order("inserted_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if rows:
            return rows[0]["snapshot_id"]
        return None
    except Exception as exc:
        log.warning("[chain_store] _find_latest_cached_snapshot error: %s", exc)
        return None
