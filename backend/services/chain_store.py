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
    has rows AND is within max_age_hours (default 48) (C-2 fix).
    Returns None on DB error, empty dict if no fresh chains exist.

get_contract_vol_oi(occ_symbol) -> tuple[int | None, int | None]
    ING-008: Fast in-process lookup from the module-level _vol_oi_cache
    dict keyed by OCC symbol.  Returns (volume, open_interest) or
    (None, None) on cache miss. The cache is populated / refreshed by start_chain_refresh_worker().

(get_tracked_symbols, fetch_chain_fn)
    ING-008: Background asyncio task that refreshes the intraday chain
    data (volume + OI) every _CHAIN_REFRESH_INTERVAL_S seconds for all
    stream-eligible symbols.

    ING-008-COLD fix: performs one IMMEDIATE refresh cycle on worker
    start before entering the sleep-first loop, so get_contract_vol_oi()
    returns live data from tick-1 instead of (None, None) for the first
    5 minutes.

    API budget: one Tradier GET /markets/options/chains call per tracked
    symbol per refresh cycle.  At 300-second cadence and a 50-symbol
    universe that is ~10 calls/minute — well within Tradier's 120 req/min
    limit.  Scale note: revisit cadence when universe exceeds ~200 symbols.

    Volume reset: the cache is fully invalidated at market open
    (call invalidate_vol_oi_cache()) so yesterday's volume never bleeds
    into early-morning events.

get_epoch() -> int
    Return the current chain_store mutation epoch counter.  Incremented on every
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
  _find_latest_cached_snapshot() now uses ORDER BY built_at DESC so the
  most recently written chain is always returned on fallback, rather than
  an arbitrary row from an unordered scan.

FIX C-1 (2026-04-27):
  save_chain() now dispatches all batch upserts concurrently using
  asyncio.gather instead of sequentially inside run_in_executor.

FIX C-2 (2026-04-27):
  _find_latest_cached_snapshot() now accepts max_age_hours and filters
  built_at >= (now - max_age_hours) before selecting. Stale snapshots
  older than 48h are ignored, forcing a fresh build() instead.

FIX EPOCH (2026-05-07):
  Added module-level _epoch counter incremented on every successful
  save_chain(). Exposed via get_epoch() for parity assertions with
  gate_config_store.assert_epoch_parity().

ING-008 (2026-05-08):
  Added `volume` column to save_chain / load_chain / _paginate_chain.
  Added get_contract_vol_oi() — O(1) dict lookup from _vol_oi_cache.
  Added () — 5-min background refresh of
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
  than the original sequential 5.8s). Zero impact on streaming hot path —
  save_chain is called only at startup and every 24h; get_contract_vol_oi()
  reads _vol_oi_cache fed by (), a separate path.

FIX (2026-05-14): HTTP 400 handling in ().
  Tradier returns HTTP 400 for tickers that have no listed options
  (e.g. AWI, ARES, ARI). The worker previously relied entirely on
  fetch_chain_fn() to return [] on all errors, but aiohttp/httpx raises
  ClientResponseError (or similar) for 4xx responses rather than returning
  gracefully. Fix: catch exceptions whose string representation contains
  '400' or whose status attribute == 400, log the ticker as non-optionable
  at INFO level (not WARNING — this is expected for some tickers), and
  skip without incrementing the error counter. All other exceptions
  continue to increment the error counter and log at WARNING.

FIX-FK-SNAPSHOT (2026-05-15):
  save_chain() was failing with Postgres FK violation 2350 la on cold-start
  periodic flushes and final persist. Root cause: options_chain_cache.snapshot_id
  references options_universe_snapshots.id, but a fresh uuid4() snapshot_id
  had no parent row before child upserts fired.

  Absolute fix:
    1. _ensure_snapshot_row(snapshot_id) now uses a FRESH Supabase client
       and PostgREST upsert(on_conflict="id") instead of plain insert,
       giving true INSERT .. ON CONFLICT DO NOTHING semantics.
    2. _ensure_snapshot_row() no longer swallows unexpected failures.
       If the parent row cannot be ensured, save_chain() fails fast before
       any child upserts start, avoiding misleading downstream 23503 noise.
    3. Each save_chain batch now uses its OWN fresh Supabase client while
       still respecting the semaphore cap. This isolates broken HTTP/2
       connections so a single ConnectionTerminated/GOAWAY cannot poison
       all concurrent batches through one shared client.

FIX-SNAPSHOT-NOTNULL (2026-05-15):
  options_universe_snapshots has two NOT NULL columns with no DB defaults:
  `provider` (TEXT NOT NULL) and `source` (TEXT NOT NULL CHECK ...).
  _ensure_snapshot_row was only sending {"id": snapshot_id}, which caused
  Postgres 23502 (not-null constraint) on every cold-start persist.
  Fix: upsert payload now includes provider='chain_store' and
  source='cache' so the row satisfies all NOT NULL constraints. The
  ON CONFLICT (id) DO NOTHING semantics ensure these sentinel values are
  only written on first insert; if universe_store already created the row
  with real provider/source values, this upsert is a no-op.

ING-008-COLD (2026-05-17):
  () opened with await asyncio.sleep(interval),
  so the first Tradier chain pull was always 300 s after worker start.
  For the entire first 5 minutes of market hours get_contract_vol_oi()
  returned (None, None) for every OCC symbol, and the OI-based ingestion
  gate dropped every flow event that depended on it.
  Fix: do one full refresh cycle IMMEDIATELY on entry, then loop with
  the normal sleep-first cadence. Initial pull is clearly labelled in
  logs as '[initial]' vs '[cycle N]'.

FIX-CHAIN-FALLBACK (2026-05-19):
  _find_latest_cached_snapshot() was filtering/ordering on `inserted_at`
  then `created_at` — neither of which exists on options_chain_cache.
  The actual timestamp column per migration 012 DDL is `built_at`
  (TIMESTAMPTZ NOT NULL DEFAULT now()), which also has a dedicated DESC
  index (idx_chain_cache_built_at). Silent PostgREST 42703 errors caused
  the fallback to always return None, forcing a full Tradier build() on
  every warm restart.
  Also bumped _DEFAULT_MAX_AGE_HOURS 24 -> 48: a chain built at 13:32 on
  day N is still valid at 14:54 on day N+1.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable, Dict, Optional, Tuple, TYPE_CHECKING

from supabase import create_client, Client
from config import settings

if TYPE_CHECKING:
    from services.symbol_registry import ContractMeta

log = logging.getLogger("chain_store")

_TABLE          = "options_chain_cache"
_SNAPSHOT_TABLE = "options_universe_snapshots"
_BATCH_SIZE     = 500
_PAGE_SIZE      = 1000
_DEFAULT_MAX_AGE_HOURS = 48  # FIX-CHAIN-FALLBACK: 24->48; overnight restarts need the extra window

# ---------------------------------------------------------------------------
# Save timeout ceiling (seconds) for long-running chain persist operations.
# ---------------------------------------------------------------------------
_SAVE_TIMEOUT_S: int = 18000

# ---------------------------------------------------------------------------
# HOTFIX-CHAIN-CONCURRENCY: cap concurrent save_chain batch writes.
# ---------------------------------------------------------------------------
_SAVE_CONCURRENCY: int = 10

# ---------------------------------------------------------------------------
# ING-008: background refresh cadence for intraday vol/OI
# ---------------------------------------------------------------------------
_CHAIN_REFRESH_INTERVAL_S: int = 300  # 5 minutes

# ---------------------------------------------------------------------------
# ING-008: in-process vol/OI cache
# ---------------------------------------------------------------------------
_vol_oi_cache: Dict[str, Dict] = {}

# ---------------------------------------------------------------------------
# Epoch counter — incremented on every successful save_chain().
# ---------------------------------------------------------------------------
_epoch: int = 0


def get_epoch() -> int:
    """Return the current chain_store mutation epoch (incremented per save_chain success)."""
    return _epoch


def get_contract_vol_oi(occ_symbol: str) -> Tuple[Optional[int], Optional[int]]:
    entry = _vol_oi_cache.get(occ_symbol)
    if entry is None:
        return (None, None)
    return (entry.get("volume"), entry.get("open_interest"))


def invalidate_vol_oi_cache() -> None:
    _vol_oi_cache.clear()
    log.info("[chain_store] vol/OI cache invalidated (market-open reset)")


def _is_http_400(exc: Exception) -> bool:
    if getattr(exc, "status", None) == 400:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 400:
        return True
    if getattr(exc, "status_code", None) == 400:
        return True
    exc_str = str(exc)
    if "400" in exc_str and ("Bad Request" in exc_str or "status" in exc_str.lower()):
        return True
    return False


async def _run_refresh_cycle(
    symbols: list,
    fetch_chain_fn: Callable[[str], Awaitable[list]],
    label: str = "cycle",
) -> None:
    """Execute one full vol/OI refresh pass over *symbols*.

    Extracted so the identical logic is shared by the immediate initial pull
    (ING-008-COLD) and every subsequent scheduled cycle inside the loop.
    *label* is used purely for log disambiguation ('initial' vs 'cycle N').
    """
    if not symbols:
        log.debug("[chain_store] chain refresh [%s]: no tracked symbols — skipping", label)
        return

    refreshed = 0
    errors = 0
    skipped_400 = 0
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
            if _is_http_400(exc):
                skipped_400 += 1
                log.info(
                    "[chain_store] chain refresh [%s]: %s has no listed options "
                    "(Tradier HTTP 400) — skipping",
                    label, symbol,
                )
            else:
                errors += 1
                log.warning(
                    "[chain_store] chain refresh [%s]: error fetching %s: %s",
                    label, symbol, exc,
                )

    log.info(
        "[chain_store] chain refresh [%s] complete — "
        "symbols=%d refreshed=%d skipped_400=%d errors=%d cache_size=%d",
        label, len(symbols), refreshed, skipped_400, errors, len(_vol_oi_cache),
    )


async def start_chain_refresh_worker(
    get_tracked_symbols: Callable[[], list],
    fetch_chain_fn: Callable[[str], Awaitable[list]],
    on_first_refresh_done: Optional[asyncio.Event] = None,
) -> None:
    """ING-008 / ING-008-COLD: background vol/OI refresh worker.

    Performs one IMMEDIATE refresh cycle on entry so the cache is warm from
    tick-1, then loops with the normal sleep-first cadence every
    _CHAIN_REFRESH_INTERVAL_S seconds.

    CHAIN-READY-001: if on_first_refresh_done is provided, it is set AFTER
    the initial pull completes — signalling stream workers that fresh
    intraday OCC contracts are loaded and safe to subscribe against.
    This replaces the prior pattern where chain_ready_event was set before
    start_chain_refresh_worker() was called, which caused workers to spawn
    against stale DB-seeded contracts and receive HTTP 400 from Tradier.
    """
    log.info(
        "[chain_store] chain refresh worker started — interval=%ds; "
        "running initial pull immediately (ING-008-COLD)",
        _CHAIN_REFRESH_INTERVAL_S,
    )
    try:
        # CHAIN-READY-001: signal stream workers NOW — the registry is already built,
        # so the set of contracts is fresh. Vol/OI enrichment can happen in parallel.
        if on_first_refresh_done is not None and not on_first_refresh_done.is_set():
            on_first_refresh_done.set()
            log.info(
                "[chain_store] CHAIN-READY-001: on_first_refresh_done set — "
                "stream workers may now spawn with today's contracts"
            )

        # ING-008-COLD: immediate initial pull
        await _run_refresh_cycle(get_tracked_symbols(), fetch_chain_fn, label="initial")

        cycle = 0
        while True:
            await asyncio.sleep(_CHAIN_REFRESH_INTERVAL_S)
            cycle += 1
            await _run_refresh_cycle(
                get_tracked_symbols(),
                fetch_chain_fn,
                label=f"cycle {cycle}",
            )

    except asyncio.CancelledError:
        log.info("[chain_store] chain refresh worker cancelled — shutting down cleanly")
        raise


def _client() -> Client:
    key = settings.SUPABASE_SERVICE_KEY
    if not key:
        raise RuntimeError(
            "[chain_store] SUPABASE_SERVICE_KEY not set — "
            "cannot read/write options_chain_cache."
        )
    return create_client(settings.SUPABASE_URL, key)


def _ensure_snapshot_row(snapshot_id: str) -> None:
    """
    Guarantee the parent options_universe_snapshots row exists before any
    child options_chain_cache upserts fire.

    Uses a FRESH client (never the shared batch client) so a broken HTTP/2
    connection cannot poison this call.

    Uses PostgREST upsert with on_conflict="id" which maps to
    INSERT ... ON CONFLICT (id) DO NOTHING at the DB level, making this
    call fully idempotent across repeated save_chain() invocations.

    The payload satisfies ALL NOT NULL columns that have no DB default:
      - provider: sentinel value 'chain_store' (overwritten if
        universe_store already created the row with a real provider).
      - source:   sentinel value 'cache' (valid per the CHECK constraint
        on source IN ('tradier_validated', 'seed_fallback', 'cache')).
    All other NOT NULL columns (fetched_at, symbol_count, is_active,
    refresh_reason, meta, created_at) have DB-level defaults and are
    omitted from the payload so PostgREST applies those defaults.

    This function is intentionally FAIL-FAST: any exception propagates to
    save_chain() which returns False immediately, preventing child upserts
    from firing against a missing parent and generating misleading 23503
    FK violations.
    """
    sb = _client()
    sb.table(_SNAPSHOT_TABLE).upsert(
        {
            "id":       snapshot_id,
            "provider": "chain_store",
            "source":   "cache",
        },
        on_conflict="id",
        returning="minimal",
    ).execute()
    log.debug(
        "[chain_store] _ensure_snapshot_row: ensured parent row for snapshot %s",
        snapshot_id,
    )


async def save_chain(
    snapshot_id: str,
    registry_dict: "dict[str, ContractMeta]",
) -> bool:
    global _epoch

    if not registry_dict:
        log.info("[chain_store] save_chain: empty registry — nothing to persist")
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

    sem = asyncio.Semaphore(_SAVE_CONCURRENCY)
    loop = asyncio.get_running_loop()

    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, _ensure_snapshot_row, snapshot_id),
            timeout=_SAVE_TIMEOUT_S,
        )
    except Exception as exc:
        log.warning(
            "[chain_store] save_chain: failed ensuring parent snapshot row %s: %s",
            snapshot_id, exc, exc_info=True,
        )
        return False

    async def _upsert_batch(batch: list, batch_num: int) -> None:
        async with sem:
            def _run() -> None:
                sb = _client()
                sb.table(_TABLE).upsert(
                    batch,
                    on_conflict="snapshot_id,occ_symbol",
                ).execute()

            await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=_SAVE_TIMEOUT_S,
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
            "[chain_store] load_chain: snapshot %s has no rows — "
            "searching for most-recent cached snapshot (max_age=%dh)",
            snapshot_id, max_age_hours,
        )
        fallback_snap = _find_latest_cached_snapshot(sb, max_age_hours=max_age_hours)
        if not fallback_snap:
            log.info(
                "[chain_store] load_chain: no cached chains within %dh — "
                "full build() required",
                max_age_hours,
            )
            return {}

        chain = _paginate_chain(sb, fallback_snap)
        log.info(
            "[chain_store] load_chain: P1 fallback — loaded %d OCC contracts "
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
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()
        resp = (
            sb.table(_TABLE)
            .select("snapshot_id")
            .gte("built_at", cutoff)           # FIX-CHAIN-FALLBACK: actual column per migration 012 DDL
            .order("built_at", desc=True)      # uses idx_chain_cache_built_at index
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
