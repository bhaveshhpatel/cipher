"""
services/universe_store.py

Supabase read/write for options universe snapshots.

Tables (must be migrated first):
  options_universe_snapshots  — one row per snapshot, only ONE is_active=true at a time
  options_universe_symbols    — normalized symbol rows per snapshot_id
                                (includes stream_eligible, last_price, volume — migration 002)
                                (includes open_interest, average_volume, tier — migration 010)

Public API:
  load_fresh_snapshot()         → list[str] | None
  load_any_snapshot()           → list[str] | None
  load_tier_map()               → dict[str, int]        [4A]
  save_snapshot(...)            → bool
  upsert_symbol_quotes(...)     → None

ROOT CAUSE FIX (2026-04-23) C-005:
  supabase-py v2 does NOT expose .select() after .insert().
  Fix: generate snapshot_id = str(uuid4()) in Python before insert.

ROOT CAUSE FIX (2026-04-23) C-006:
  options_universe_snapshots.provider is NOT NULL with no default.
  Fix: always pass provider="tradier" explicitly.

ROOT CAUSE FIX (2026-04-23) C-007:
  _client() was falling back to the anon key (SUPABASE_KEY) when
  SUPABASE_SERVICE_KEY was not set. The anon key respects RLS and causes
  42501 policy violations on every server-side INSERT/UPDATE/DELETE.
  Fix: use SUPABASE_SERVICE_KEY ONLY — raise clearly if it is missing.

Feature 4A-OI (2026-04-25):
  open_interest column (migration 010) is now written by upsert_symbol_quotes()
  with the avg chain OI value from registry.get_oi_map(), populated by main.py.
  This column is no longer NULL after the first full startup cycle.

FIX (2026-04-27a):
  _load_symbols() now filters on stream_eligible=True so warm restarts
  only pass the price/volume-filtered pool to the registry, not the full
  ~5,270 raw CBOE dump.

FIX (2026-04-27b):
  _load_symbols() and _sync_load_tier_map() now paginate in _PAGE_SIZE
  chunks to bypass Supabase PostgREST's silent 1000-row cap. Previously
  every warm-start was truncated at exactly 1000 symbols regardless of
  how many stream_eligible rows were in the snapshot.

FIX (2026-04-27c):
  _sync_save_snapshot: changed options_universe_symbols insert → upsert
  (on_conflict="snapshot_id,symbol") so repeated runs under the same
  snapshot_id are idempotent and never produce duplicate rows.

FIX (2026-04-27d):
  _prune_old_snapshots: explicitly DELETE options_universe_symbols rows
  for pruned snapshot IDs before deleting the snapshot header. Safety net
  for DBs without a cascading FK — prevents orphaned symbol rows from
  accumulating across restarts.

FIX RC-1/RC-2 (2026-04-27e):
  _sync_save_snapshot now inserts ONLY stream_eligible symbols instead of
  the full CBOE dump (~5270). Previously all symbols were inserted with
  stream_eligible flag set per row, but non-eligible rows were never
  updated by upsert_symbol_quotes(), leaving last_price=NULL and
  open_interest=NULL permanently on ~912 rows.
  - symbol_count in snapshot header is now set to len(eligible rows) not
    len(all symbols), so S-04 and S-05 pass correctly.
  - Non-eligible symbols simply have no row in options_universe_symbols.

FIX (2026-04-28) SNAPSHOT-REUSE:
  Root cause of exponential row growth in options_universe_symbols:
  Every deployment called uuid4() unconditionally, producing a fresh
  snapshot_id. The upsert on_conflict=(snapshot_id,symbol) never found
  a conflict because the key was always new — so every restart was a
  pure INSERT storm, not an idempotent upsert.

  Fix: _sync_save_snapshot now checks for an existing active snapshot
  created within _SNAPSHOT_REUSE_MAX_AGE_H (20h) with the same source.
  If found, its ID is reused so subsequent upserts truly deduplicate.
  A brand-new uuid4() is only minted when:
    - No active snapshot exists, OR
    - The existing one is older than 20 hours (stale → refresh), OR
    - The source tag differs (forced full refresh).
  Also fixes options_chain_cache exponential growth (same root cause —
  chain rows keyed on snapshot_id also multiplied every restart).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

from supabase import create_client, Client
from config import settings

if TYPE_CHECKING:
    from services.symbols_loader import SymbolQuote

log = logging.getLogger("universe_store")

_KEEP_SNAPSHOTS          = 7
_DEFAULT_MAX_AGE         = 24   # hours
_UPSERT_BATCH            = 500  # rows per upsert batch
_PAGE_SIZE               = 1000  # PostgREST default cap — paginate in this chunk size
_SNAPSHOT_REUSE_MAX_AGE_H = 20  # hours — reuse active snapshot_id within this window


def _client() -> Client:
    """
    Always use the service role key — it bypasses RLS, which is required
    for all server-side INSERT/UPDATE/DELETE operations.

    NEVER fall back to the anon key (settings.SUPABASE_KEY). The anon key
    respects RLS and will cause 401/42501 errors on every write.
    """
    service_key = settings.SUPABASE_SERVICE_KEY
    if not service_key:
        raise RuntimeError(
            "[universe_store] SUPABASE_SERVICE_KEY is not set. "
            "Set it in Railway env vars to the Supabase service_role key. "
            "Never use the anon key for backend DB writes."
        )
    return create_client(settings.SUPABASE_URL, service_key)


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def load_fresh_snapshot(max_age_hours: int = _DEFAULT_MAX_AGE) -> Optional[list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_fresh_snapshot, max_age_hours)


async def load_any_snapshot() -> Optional[list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_any_snapshot)


async def load_tier_map() -> dict[str, int]:
    """
    Return dict[symbol -> tier] from the current active snapshot.
    Used by main.py on warm starts (Step 1 HIT) to seed init_registry()
    with accurate per-symbol tiers without re-running the full pipeline.
    Returns empty dict on error or if no active snapshot exists.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_tier_map)


async def save_snapshot(
    symbols: list[str],
    source: str,
    stream_eligible_set: Optional[set[str]] = None,
) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_save_snapshot, symbols, source, stream_eligible_set
    )


async def upsert_symbol_quotes(
    quotes: list["SymbolQuote"],
    tier_map: Optional[dict[str, int]] = None,
) -> None:
    """
    Persist Step 3 quote data (last_price, volume, average_volume,
    open_interest, tier, stream_eligible) for each symbol into the
    most recent active snapshot.

    open_interest: avg chain OI per ticker, populated on the quote by
    main.py from registry.get_oi_map() before this is called (Feature 4A-OI).

    tier_map: dict[symbol -> tier] from tier_engine.assign_tiers().
    If not provided, all symbols default to tier=3.

    Uses ON CONFLICT (snapshot_id, symbol) DO UPDATE so it is safe to call
    before or after save_snapshot() — whichever order, the data will be consistent.

    Non-fatal: logs a warning and returns if no active snapshot exists yet.
    """
    if not quotes:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_upsert_symbol_quotes, quotes, tier_map or {})


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

def _paginate_symbols(
    sb: Client,
    snapshot_id: str,
    select_cols: str,
    extra_filters: Optional[dict] = None,
) -> list[dict]:
    """
    Fetch ALL rows from options_universe_symbols for a given snapshot_id,
    paginating in _PAGE_SIZE chunks to bypass PostgREST's 1000-row default cap.

    extra_filters: dict of {column: value} applied as .eq(col, val) filters.
    Returns the full list of row dicts.
    """
    all_rows: list[dict] = []
    offset = 0
    while True:
        q = (
            sb.table("options_universe_symbols")
            .select(select_cols)
            .eq("snapshot_id", snapshot_id)
        )
        if extra_filters:
            for col, val in extra_filters.items():
                q = q.eq(col, val)
        result = (
            q
            .order("symbol")
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        page = result.data or []
        all_rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return all_rows


# ---------------------------------------------------------------------------
# Sync implementations
# ---------------------------------------------------------------------------

def _sync_load_fresh_snapshot(max_age_hours: int) -> Optional[list[str]]:
    try:
        sb     = _client()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        log.info("universe_store: querying fresh snapshot (cutoff=%s)", cutoff)
        result = (
            sb.table("options_universe_snapshots")
            .select("id, fetched_at")
            .eq("is_active", True)
            .gte("fetched_at", cutoff)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            log.info("universe_store: no fresh active snapshot found")
            return None
        snapshot_id = rows[0]["id"]
        fetched_at  = rows[0]["fetched_at"]
        log.info("universe_store: fresh snapshot found id=%s fetched_at=%s", snapshot_id, fetched_at)
        return _load_symbols(sb, snapshot_id)
    except Exception as e:
        log.error("universe_store.load_fresh_snapshot error: %s", e, exc_info=True)
        return None


def _sync_load_any_snapshot() -> Optional[list[str]]:
    try:
        sb = _client()
        log.info("universe_store: querying any snapshot (stale fallback)")
        result = (
            sb.table("options_universe_snapshots")
            .select("id, fetched_at, source")
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            log.info("universe_store: no snapshots in DB at all")
            return None
        snapshot_id = rows[0]["id"]
        fetched_at  = rows[0]["fetched_at"]
        source      = rows[0]["source"]
        log.info(
            "universe_store: loading stale snapshot id=%s source=%s fetched_at=%s",
            snapshot_id, source, fetched_at,
        )
        return _load_symbols(sb, snapshot_id)
    except Exception as e:
        log.error("universe_store.load_any_snapshot error: %s", e, exc_info=True)
        return None


def _sync_load_tier_map() -> dict[str, int]:
    """
    Load symbol -> tier mapping from the current active snapshot.
    Paginates in _PAGE_SIZE chunks to bypass Supabase's 1000-row cap.
    Symbols with NULL or missing tier column default to 3.
    Returns {} on error or missing snapshot.
    """
    try:
        sb = _client()

        snap = (
            sb.table("options_universe_snapshots")
            .select("id")
            .eq("is_active", True)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = snap.data or []
        if not rows:
            log.info("universe_store.load_tier_map: no active snapshot")
            return {}

        snapshot_id = rows[0]["id"]

        all_rows = _paginate_symbols(
            sb, snapshot_id,
            select_cols="symbol, tier",
            extra_filters={"stream_eligible": True},
        )

        tier_map = {
            r["symbol"]: int(r.get("tier") or 3)
            for r in all_rows
            if r.get("symbol")
        }
        log.info(
            "universe_store.load_tier_map: loaded %d tiers from snapshot %s (T1=%d T2=%d T3=%d)",
            len(tier_map), snapshot_id,
            sum(1 for t in tier_map.values() if t == 1),
            sum(1 for t in tier_map.values() if t == 2),
            sum(1 for t in tier_map.values() if t == 3),
        )
        return tier_map
    except Exception as e:
        log.warning("universe_store.load_tier_map error (non-fatal): %s", e, exc_info=True)
        return {}


def _load_symbols(sb: Client, snapshot_id: str) -> Optional[list[str]]:
    """
    Load only stream-eligible symbols from the snapshot, paginating in
    _PAGE_SIZE chunks to bypass Supabase PostgREST's silent 1000-row cap.

    Filtering on stream_eligible=True ensures warm restarts pass the
    price/volume-filtered pool (~1000-2000 symbols) to the registry
    rather than the full ~5,270 raw CBOE dump.
    """
    try:
        all_rows = _paginate_symbols(
            sb, snapshot_id,
            select_cols="symbol",
            extra_filters={"stream_eligible": True},
        )
        symbols = [r["symbol"] for r in all_rows if r.get("symbol")]
        log.info(
            "universe_store: loaded %d stream-eligible symbols from snapshot %s",
            len(symbols), snapshot_id,
        )
        return symbols if symbols else None
    except Exception as e:
        log.error("universe_store._load_symbols error snapshot=%s: %s", snapshot_id, e, exc_info=True)
        return None


def _get_reusable_snapshot_id(sb: Client, source: str) -> Optional[str]:
    """
    Return the ID of the current active snapshot if it was created within
    _SNAPSHOT_REUSE_MAX_AGE_H hours AND has the same source tag.

    If found, _sync_save_snapshot will upsert into this existing snapshot
    rather than minting a new uuid4(), making repeated deployments on the
    same trading day fully idempotent (no duplicate rows).

    Returns None when a brand-new snapshot_id should be generated:
      - No active snapshot exists
      - Active snapshot is older than _SNAPSHOT_REUSE_MAX_AGE_H
      - Source tag differs (e.g. cboe → tradier forced refresh)
    """
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=_SNAPSHOT_REUSE_MAX_AGE_H)
        ).isoformat()
        result = (
            sb.table("options_universe_snapshots")
            .select("id, fetched_at, source")
            .eq("is_active", True)
            .eq("source", source)
            .gte("fetched_at", cutoff)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows:
            sid = rows[0]["id"]
            log.info(
                "universe_store: reusing existing snapshot_id=%s (fetched_at=%s, source=%s) "
                "— upsert will be idempotent, no duplicate rows",
                sid, rows[0]["fetched_at"], source,
            )
            return sid
        return None
    except Exception as e:
        log.warning("universe_store._get_reusable_snapshot_id error (non-fatal): %s", e)
        return None


def _sync_save_snapshot(
    symbols: list[str],
    source: str,
    stream_eligible_set: Optional[set[str]] = None,
) -> bool:
    """
    Save or update the options universe snapshot.

    SNAPSHOT-REUSE FIX (2026-04-28):
    Before minting a new uuid4(), check _get_reusable_snapshot_id().
    If an active snapshot with the same source exists and is < 20h old,
    reuse its ID. This means the upsert on_conflict=(snapshot_id,symbol)
    will actually find existing rows and UPDATE them instead of always
    inserting new rows.

    RC-1/RC-2 FIX: Only insert stream_eligible symbols into
    options_universe_symbols. Previously ALL ~5270 CBOE symbols were
    inserted regardless of eligibility, causing:
      - S-04: symbol count inflated to 5252 instead of ~4340
      - S-05: 913 rows with last_price=NULL (non-eligible rows never
              touched by upsert_symbol_quotes)
      - S-12: 2637 tickers with no chain data (non-eligible but stored)

    Now:
      - eligible_symbols = intersection of symbols and stream_eligible_set
      - symbol_count in snapshot header = len(eligible_symbols)
      - Non-eligible symbols have zero rows in options_universe_symbols

    1. Try to reuse existing active snapshot_id (SNAPSHOT-REUSE)
    2. If no reusable snapshot, generate a new uuid4() and INSERT header
    3. Upsert ONLY eligible symbols in batches of 500
    4. Deactivate all other snapshots (only when snapshot_id is new)
    5. Prune beyond _KEEP_SNAPSHOTS
    """
    if not symbols:
        log.warning("universe_store.save_snapshot: called with empty symbol list — skipping")
        return False
    try:
        sb = _client()

        # RC-1: only persist stream_eligible rows
        eligible_set     = stream_eligible_set if stream_eligible_set is not None else set(symbols)
        eligible_symbols = [s for s in symbols if s in eligible_set]

        if not eligible_symbols:
            log.warning(
                "universe_store.save_snapshot: stream_eligible_set produced 0 eligible symbols "
                "from %d total — writing all symbols as fallback",
                len(symbols),
            )
            eligible_symbols = list(symbols)

        # SNAPSHOT-REUSE: reuse existing snapshot_id when possible so upserts
        # are truly idempotent across restarts on the same trading day.
        reused_id    = _get_reusable_snapshot_id(sb, source)
        is_new_snap  = reused_id is None
        snapshot_id  = reused_id if reused_id else str(uuid4())

        if is_new_snap:
            log.info(
                "universe_store: creating NEW snapshot id=%s source=%s "
                "eligible=%d (of %d total symbols)",
                snapshot_id, source, len(eligible_symbols), len(symbols),
            )
            sb.table("options_universe_snapshots").insert({
                "id":           snapshot_id,
                "symbol_count": len(eligible_symbols),
                "provider":     "tradier",
                "source":       source,
                "is_active":    True,
            }).execute()
        else:
            log.info(
                "universe_store: REUSING snapshot id=%s source=%s — "
                "upserting %d eligible symbols (idempotent)",
                snapshot_id, source, len(eligible_symbols),
            )
            # Update symbol_count in case the eligible set changed slightly
            sb.table("options_universe_snapshots").update({
                "symbol_count": len(eligible_symbols),
            }).eq("id", snapshot_id).execute()

        rows = [
            {
                "snapshot_id":     snapshot_id,
                "symbol":          s,
                "stream_eligible": True,  # all rows are eligible by construction
                "tier":            3,
            }
            for s in eligible_symbols
        ]
        total_batches = (len(rows) + _UPSERT_BATCH - 1) // _UPSERT_BATCH
        for i in range(0, len(rows), _UPSERT_BATCH):
            batch_num = i // _UPSERT_BATCH + 1
            sb.table("options_universe_symbols").upsert(
                rows[i : i + _UPSERT_BATCH],
                on_conflict="snapshot_id,symbol",
            ).execute()
            log.info(
                "universe_store: upserted symbol batch %d/%d (%d symbols)",
                batch_num, total_batches, len(rows[i : i + _UPSERT_BATCH]),
            )

        # Only deactivate other snapshots when we created a new one.
        # When reusing, there's nothing to deactivate.
        if is_new_snap:
            sb.table("options_universe_snapshots").update({"is_active": False}).neq(
                "id", snapshot_id
            ).execute()
            log.info("universe_store: deactivated previous snapshots")

        log.info(
            "universe_store: snapshot SAVED id=%s new=%s eligible_symbols=%d source=%s",
            snapshot_id, is_new_snap, len(eligible_symbols), source,
        )

        _prune_old_snapshots(sb, keep=_KEEP_SNAPSHOTS)
        return True

    except Exception as e:
        log.error("universe_store.save_snapshot error: %s", e, exc_info=True)
        return False


def _sync_upsert_symbol_quotes(quotes: list, tier_map: dict) -> None:
    """
    Upsert last_price, volume, average_volume, open_interest, tier,
    stream_eligible for every symbol in the active snapshot.
    """
    try:
        sb = _client()

        result = (
            sb.table("options_universe_snapshots")
            .select("id")
            .eq("is_active", True)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            log.warning(
                "upsert_symbol_quotes: no active snapshot found — "
                "quote data not persisted (will be available after save_snapshot)"
            )
            return

        snapshot_id = rows[0]["id"]
        log.info(
            "upsert_symbol_quotes: upserting %d symbol quotes into snapshot %s",
            len(quotes), snapshot_id,
        )

        upsert_rows = [
            {
                "snapshot_id":     snapshot_id,
                "symbol":          q.symbol,
                "last_price":      q.last_price,
                "volume":          q.volume,
                "average_volume":  q.average_volume,
                "open_interest":   q.open_interest,
                "stream_eligible": q.stream_eligible,
                "tier":            tier_map.get(q.symbol, 3),
            }
            for q in quotes
        ]

        total_batches = (len(upsert_rows) + _UPSERT_BATCH - 1) // _UPSERT_BATCH
        for i in range(0, len(upsert_rows), _UPSERT_BATCH):
            batch_num = i // _UPSERT_BATCH + 1
            sb.table("options_universe_symbols").upsert(
                upsert_rows[i : i + _UPSERT_BATCH],
                on_conflict="snapshot_id,symbol",
            ).execute()
            log.info(
                "upsert_symbol_quotes: batch %d/%d (%d rows)",
                batch_num, total_batches, len(upsert_rows[i : i + _UPSERT_BATCH]),
            )

        log.info("upsert_symbol_quotes: complete")

    except Exception as e:
        log.warning("upsert_symbol_quotes error (non-fatal): %s", e, exc_info=True)


def _prune_old_snapshots(sb: Client, keep: int) -> None:
    try:
        all_snaps = (
            sb.table("options_universe_snapshots")
            .select("id")
            .order("fetched_at", desc=True)
            .execute()
        )
        rows = all_snaps.data or []
        if len(rows) <= keep:
            return
        ids_to_delete = [r["id"] for r in rows[keep:]]
        # Delete child symbol rows first — safety net for DBs without cascading FK.
        # Prevents orphaned options_universe_symbols rows from accumulating.
        sb.table("options_universe_symbols").delete().in_("snapshot_id", ids_to_delete).execute()
        sb.table("options_universe_snapshots").delete().in_("id", ids_to_delete).execute()
        log.info("universe_store: pruned %d old snapshots (+ their symbol rows)", len(ids_to_delete))
    except Exception as e:
        log.warning("universe_store._prune_old_snapshots error (non-fatal): %s", e)
