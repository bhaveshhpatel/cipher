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
  run_in_executor threads. Wall time: ~300ms → ~1.5s (still 4x faster
  than the original sequential 5.8s). Zero impact on streaming hot path —
  save_chain is called only at startup and every 24h; get_contract_vol_oi()
  reads _vol_oi_cache fed by start_chain_refresh_worker(), a separate path.

FIX (2026-05-14): HTTP 400 handling in start_chain_refresh_worker().
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
  save_chain() was failing with Postgres FK violation 23503 on every cold-start
  periodic flush and final persist. Root cause: options_chain_cache.snapshot_id
  has a FK constraint referencing options_universe_snapshots.id. When
  _periodic_flush() generated a fresh uuid4() (after FIX-PARTIAL-UUID), no
  corresponding parent row existed in options_universe_snapshots, so every
  upsert into options_chain_cache was rejected.

  Fix: _ensure_snapshot_row(sb, snapshot_id) is called once at the top of
  save_chain() before any batch upserts. It executes:
    INSERT INTO options_universe_snapshots (id) VALUES (:id) ON CONFLICT DO NOTHING
  On cold start this creates the parent row so all subsequent
  options_chain_cache upserts satisfy the FK. On subsequent flushes (row
  already exists) it is a true no-op — zero writes, zero contention.
  The helper is synchronous (called via run_in_executor alongside batch
  upserts) and non-fatal: any error is logged as WARNING and save_chain
  continues (same contract as existing batch error handling).
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
_DEFAULT_MAX_AGE_HOURS = 24

# ---------------------------------------------------------------------------
# HOTFIX-CHAIN-CONCURRENCY: cap concurrent save_chain batch writes.
# Previously all batches (up to 155) fired simultaneously, each creating
# its own Supabase client. 10 concurrent batches keeps memory flat and
# avoids threadpool saturation while still being ~4x faster than sequential.
# ---------------------------------------------------------------------------
_SAVE_CONCURRENCY: int = 10

# ---------------------------------------------------------------------------
# ING-008: background refresh cadence for intraday vol/OI
# One Tradier chain call per symbol per cycle.
# At 300s cadence and 50 symbols → ~10 req/min (limit: 120 req/min).
# Revisit when universe exceeds ~200 symbols.
# ---------------------------------------------------------------------------
_CHAIN_REFRESH_INTERVAL_S: int = 300  # 5 minutes

# ---------------------------------------------------------------------------
# ING-008: in-process vol/OI cache
#   Keyed by OCC symbol string.
#   Value: {"volume": int, "open_interest": int, "refreshed_at": float}
#   Populated / refreshed by start_chain_refresh_worker().
#   Invalidated at market open via invalidate_vol_oi_cache().
# ---------------------------------------------------------------------------
_vol_oi_cache: Dict[str, Dict] = {}

# ---------------------------------------------------------------------------
# Epoch counter — incremented on every successful save_chain().
# Starts at 0 (pre-first-save); get_epoch() exposes it publicly.
# ---------------------------------------------------------------------------
_epoch: int = 0


def get_epoch() -> int:
    """Return the current chain_store mutation epoch (incremented per save_chain success)."""
    return _epoch


def get_contract_vol_oi(occ_symbol: str) -> Tuple[Optional[int], Optional[int]]:
    """
    ING-008: O(1) in-process lookup of (volume, open_interest) for an OCC symbol.

    Returns (volume, open_interest) from the _vol_oi_cache populated by
    start_chain_refresh_worker().  Returns (None, None) on cache miss.

    Never makes a live API call — zero latency on the hot path.
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
    volume never bleeds into early-morning flow events.
    Call this from the market-open boundary handler in main.py or the stream.
    """
    _vol_oi_cache.clear()
    log.info("[chain_store] vol/OI cache invalidated (market-open reset)")


def _is_http_400(exc: Exception) -> bool:
    """
    FIX (2026-05-14): Detect HTTP 400 responses from any HTTP client library
    (aiohttp, httpx, requests) without hard-coding a specific exception type.

    Checks:
      1. exc.status == 400         (aiohttp ClientResponseError)
      2. exc.status_code == 400    (httpx HTTPStatusError)
      3. exc.response.status_code == 400  (requests HTTPError / httpx variant)
      4. '400' in str(exc)         (last resort string match)

    Returns True only when we are confident this is a 400 Bad Request,
    so legitimate network errors still route to the WARNING path.
    """
    # aiohttp: ClientResponseError.status
    if getattr(exc, "status", None) == 400:
        return True
    # httpx: HTTPStatusError.response.status_code
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 400:
        return True
    # httpx direct attribute
    if getattr(exc, "status_code", None) == 400:
        return True
    # Last-resort string match — only when string is short and contains '400'
    # to avoid false positives on messages mentioning port 4000 etc.
    exc_str = str(exc)
    if "400" in exc_str and ("Bad Request" in exc_str or "status" in exc_str.lower()):
        return True
    return False


async def start_chain_refresh_worker(
    get_tracked_symbols: Callable[[], list],
    fetch_chain_fn: Callable[[str], Awaitable[list]],
) -> None:
    """
    ING-008: Background asyncio task that refreshes intraday chain data
    (volume + OI per OCC symbol) for all stream-eligible tracked symbols.

    Runs every _CHAIN_REFRESH_INTERVAL_S seconds (default: 300s / 5 min).

    Parameters
    ----------
    get_tracked_symbols : Callable[[], list]
        Zero-argument callable that returns the current list of tracked
        ticker symbols (e.g. lambda: list(registry.tickers())).  Called
        fresh on each cycle so newly added symbols are picked up.

    fetch_chain_fn : Callable[[str], Awaitable[list]]
        Async callable that accepts a ticker symbol string and returns a
        list of contract dicts from Tradier GET /markets/options/chains.
        Each dict must contain at minimum:
          - "symbol"        : str  (OCC symbol)
          - "volume"        : int
          - "open_interest" : int
        On error it should return [] (never raise) so one bad symbol does
        not abort the entire refresh cycle.

    HTTP 400 handling (FIX 2026-05-14)
    ------------------------------------
    Tradier returns HTTP 400 for tickers with no listed options contracts
    (e.g. AWI, ARES, ARI). These 400s are expected and non-actionable —
    the ticker just isn't optionable. They are caught by _is_http_400(),
    logged at INFO (not WARNING — no action required), and skipped without
    incrementing the error counter. All other exceptions are logged at
    WARNING and DO increment the error counter.

    API budget
    ----------
    One call per symbol per cycle.  At 300s cadence, 50 symbols → 10 req/min.
    Tradier rate limit: 120 req/min.  Headroom: ~110 req/min for stream.
    Scale note: if symbol universe exceeds ~200, increase interval to 600s.

    Cache population
    ----------------
    Writes directly into _vol_oi_cache[occ_symbol] = {
        "volume": int, "open_interest": int, "refreshed_at": epoch_float
    }.
    flow_store.persist_flow_event and persist_flow_episode call
    get_contract_vol_oi(occ_symbol) for a zero-API-call lookup.

    Volume reset
    ------------
    _vol_oi_cache is NOT cleared here between cycles — it is invalidated
    at market open by calling invalidate_vol_oi_cache() from the stream
    market-open handler.  This prevents yesterday's volume from bleeding
    into pre-market / early-morning events.
    """
    log.info(
        "[chain_store] chain refresh worker started — interval=%ds",
        _CHAIN_REFRESH_INTERVAL_S,
    )
    try:
        while True:
            await asyncio.sleep(_CHAIN_REFRESH_INTERVAL_S)
            symbols = get_tracked_symbols()
            if not symbols:
                log.debug("[chain_store] chain refresh: no tracked symbols yet, skipping cycle")
                continue

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
                    # FIX (2026-05-14): Tradier returns HTTP 400 for non-optionable
                    # tickers (AWI, ARES, ARI, etc.). Detect these specifically so
                    # they do not pollute the error counter or log at WARNING level.
                    if _is_http_400(exc):
                        skipped_400 += 1
                        log.info(
                            "[chain_store] chain refresh: %s has no listed options "
                            "(Tradier HTTP 400) — skipping",
                            symbol,
                        )
                    else:
                        errors += 1
                        log.warning(
                            "[chain_store] chain refresh: error fetching %s: %s",
                            symbol, exc,
                        )

            log.info(
                "[chain_store] chain refresh cycle complete — "
                "symbols=%d refreshed=%d skipped_400=%d errors=%d cache_size=%d",
                len(symbols), refreshed, skipped_400, errors, len(_vol_oi_cache),
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


def _ensure_snapshot_row(sb: Client, snapshot_id: str) -> None:
    """
    FIX-FK-SNAPSHOT: Insert a parent row into options_universe_snapshots
    so that subsequent options_chain_cache upserts satisfy the FK constraint
    options_chain_cache_snapshot_id_fkey.

    Uses ON CONFLICT DO NOTHING so this is a true no-op when the row already
    exists (e.g. second periodic flush, or final _persist_to_db() call after
    _periodic_flush() already created the row).

    Non-fatal: any error is logged as WARNING. save_chain() proceeds and the
    FK violation will surface naturally in the batch upsert error handling.
    """
    try:
        sb.table(_SNAPSHOT_TABLE).insert(
            {"id": snapshot_id},
            returning="minimal",
        ).execute()
        log.debug(
            "[chain_store] _ensure_snapshot_row: upserted parent row for snapshot %s",
            snapshot_id,
        )
    except Exception as exc:
        # PostgREST / Supabase raises on duplicate key — treat as a no-op.
        # Any genuine error (wrong table name, auth failure) will also surface
        # in the batch upserts below with a clearer message.
        exc_str = str(exc)
        if "duplicate" in exc_str.lower() or "23505" in exc_str or "conflict" in exc_str.lower():
            log.debug(
                "[chain_store] _ensure_snapshot_row: snapshot %s already exists — no-op",
                snapshot_id,
            )
        else:
            log.warning(
                "[chain_store] _ensure_snapshot_row: unexpected error for snapshot %s: %s",
                snapshot_id, exc,
            )


async def save_chain(
    snapshot_id: str,
    registry_dict: "dict[str, ContractMeta]",
) -> bool:
    """
    HOTFIX-CHAIN-CONCURRENCY + C-1: Persist all ContractMeta rows with
    bounded concurrency via a single shared Supabase client.

    Previously _client() was called inside _upsert_batch, creating one new
    Supabase connection pool per batch. With 155 batches firing concurrently
    via asyncio.gather, this instantiated 155 clients simultaneously — spiking
    memory and saturating the threadpool, causing OOM restarts and health probe
    failures on Render.

    Fix:
      - ONE _client() call outside the batch loop, shared by all batches.
      - asyncio.Semaphore(_SAVE_CONCURRENCY=10) caps concurrent
        run_in_executor threads to 10 at a time.

    Wall time: ~300ms (155 concurrent) → ~1.5s (10 at a time).
    Still ~4x faster than the original sequential 5.8s path.
    Zero impact on streaming: save_chain is called only at startup and
    every 24h; the hot-path vol/OI lookups use _vol_oi_cache exclusively.

    FIX-FK-SNAPSHOT: _ensure_snapshot_row() is called once before any batch
    upserts to guarantee the parent row in options_universe_snapshots exists.
    Without this, every upsert fails with Postgres FK violation 23503.

    Increments the module-level _epoch counter on success.
    """
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

    # HOTFIX-CHAIN-CONCURRENCY: single client shared across all batches.
    sb  = _client()
    sem = asyncio.Semaphore(_SAVE_CONCURRENCY)

    # FIX-FK-SNAPSHOT: ensure the parent options_universe_snapshots row
    # exists before any child options_chain_cache upserts fire.
    # This satisfies the FK constraint options_chain_cache_snapshot_id_fkey
    # on both cold-start periodic flushes and the final _persist_to_db() call.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _ensure_snapshot_row, sb, snapshot_id)

    async def _upsert_batch(batch: list, batch_num: int) -> None:
        async with sem:
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
        # ING-008: attach volume if the ContractMeta dataclass supports it;
        # use setattr so this is non-breaking if the field hasn't been added yet.
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

    Previously returned ANY snapshot regardless of age, causing expired
    contracts (DTE=0 or negative) to silently load on warm restart when
    a chain from >24h ago was the only cached snapshot.

    Now filters inserted_at >= (now - max_age_hours) before selecting.
    Returns None if no snapshot within the window exists, forcing a fresh
    build() rather than loading stale data.
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
