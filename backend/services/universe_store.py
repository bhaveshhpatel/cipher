"""
services/universe_store.py

Supabase read/write for options universe snapshots.

Tables (must be migrated first):
  options_universe_snapshots  — one row per snapshot, only ONE is_active=true at a time
  options_universe_symbols    — normalized symbol rows per snapshot_id
                                (includes stream_eligible, last_price, volume — migration 002)

Public API:
  load_fresh_snapshot()  → list[str] | None
  load_any_snapshot()    → list[str] | None
  save_snapshot(symbols, source, stream_eligible_set)  → bool
  upsert_symbol_quotes(quotes)  → None   ← NEW (Step 3)

ROOT CAUSE FIX (2026-04-23) C-005:
  supabase-py v2 does NOT expose .select() after .insert().
  Fix: generate snapshot_id = str(uuid4()) in Python before insert.

ROOT CAUSE FIX (2026-04-23) C-006:
  options_universe_snapshots.provider is NOT NULL with no default.
  Fix: always pass provider="tradier" explicitly.
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

_KEEP_SNAPSHOTS  = 7
_DEFAULT_MAX_AGE = 24   # hours
_UPSERT_BATCH    = 500  # rows per upsert batch


def _client() -> Client:
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY,
    )


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def load_fresh_snapshot(max_age_hours: int = _DEFAULT_MAX_AGE) -> Optional[list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_fresh_snapshot, max_age_hours)


async def load_any_snapshot() -> Optional[list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_any_snapshot)


async def save_snapshot(
    symbols: list[str],
    source: str,
    stream_eligible_set: Optional[set[str]] = None,
) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_save_snapshot, symbols, source, stream_eligible_set
    )


async def upsert_symbol_quotes(quotes: list["SymbolQuote"]) -> None:
    """
    Persist Step 3 quote data (last_price, volume, stream_eligible) for each symbol
    into the most recent active snapshot row for that symbol.

    Uses ON CONFLICT (snapshot_id, symbol) DO UPDATE so it is safe to call
    before or after save_snapshot() — whichever order, the data will be consistent.

    Non-fatal: logs a warning and returns if no active snapshot exists yet.
    """
    if not quotes:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_upsert_symbol_quotes, quotes)


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


def _load_symbols(sb: Client, snapshot_id: str) -> Optional[list[str]]:
    try:
        result = (
            sb.table("options_universe_symbols")
            .select("symbol")
            .eq("snapshot_id", snapshot_id)
            .execute()
        )
        rows    = result.data or []
        symbols = [r["symbol"] for r in rows if r.get("symbol")]
        log.info("universe_store: loaded %d symbols from snapshot %s", len(symbols), snapshot_id)
        return symbols if symbols else None
    except Exception as e:
        log.error("universe_store._load_symbols error snapshot=%s: %s", snapshot_id, e, exc_info=True)
        return None


def _sync_save_snapshot(
    symbols: list[str],
    source: str,
    stream_eligible_set: Optional[set[str]] = None,
) -> bool:
    """
    1. Generate snapshot_id locally via uuid4()
    2. Insert snapshot header
    3. Bulk-insert symbols in batches of 500 — includes stream_eligible flag
    4. Deactivate all other snapshots
    5. Prune beyond _KEEP_SNAPSHOTS
    """
    if not symbols:
        log.warning("universe_store.save_snapshot: called with empty symbol list — skipping")
        return False
    try:
        sb          = _client()
        snapshot_id = str(uuid4())

        log.info(
            "universe_store: inserting snapshot id=%s source=%s symbols=%d stream_eligible=%s",
            snapshot_id, source, len(symbols),
            len(stream_eligible_set) if stream_eligible_set is not None else "all",
        )

        # 1. Insert snapshot header
        sb.table("options_universe_snapshots").insert({
            "id":           snapshot_id,
            "symbol_count": len(symbols),
            "provider":     "tradier",
            "source":       source,
            "is_active":    True,
        }).execute()

        # 2. Bulk insert symbols with stream_eligible flag
        # NOTE: last_price and volume will be null at insert time;
        # they are populated by upsert_symbol_quotes() called from load_universe().
        # The stream_eligible flag here uses the set computed by _fetch_batch_quotes().
        batch_size    = 500
        eligible_set  = stream_eligible_set if stream_eligible_set is not None else set(symbols)
        rows          = [
            {
                "snapshot_id":     snapshot_id,
                "symbol":          s,
                "stream_eligible": s in eligible_set,
            }
            for s in symbols
        ]
        total_batches = (len(rows) + batch_size - 1) // batch_size
        for i in range(0, len(rows), batch_size):
            batch_num = i // batch_size + 1
            sb.table("options_universe_symbols").insert(rows[i : i + batch_size]).execute()
            log.info(
                "universe_store: inserted symbol batch %d/%d (%d symbols)",
                batch_num, total_batches, len(rows[i : i + batch_size]),
            )

        # 3. Deactivate all other snapshots
        sb.table("options_universe_snapshots").update({"is_active": False}).neq(
            "id", snapshot_id
        ).execute()
        log.info("universe_store: deactivated previous snapshots")

        log.info(
            "universe_store: snapshot SAVED id=%s symbols=%d source=%s",
            snapshot_id, len(symbols), source,
        )

        # 4. Prune old snapshots
        _prune_old_snapshots(sb, keep=_KEEP_SNAPSHOTS)
        return True

    except Exception as e:
        log.error("universe_store.save_snapshot error: %s", e, exc_info=True)
        return False


def _sync_upsert_symbol_quotes(quotes: list) -> None:
    """
    Upsert last_price, volume, stream_eligible for every symbol in the active snapshot.

    Looks up the active snapshot_id first, then upserts in batches.
    Safe to call multiple times — uses ON CONFLICT DO UPDATE semantics via
    supabase-py's .upsert() with on_conflict='snapshot_id,symbol'.
    """
    try:
        sb = _client()

        # Find the current active snapshot
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

        # Build upsert rows
        upsert_rows = [
            {
                "snapshot_id":     snapshot_id,
                "symbol":          q.symbol,
                "last_price":      q.last_price,
                "volume":          q.volume,
                "stream_eligible": q.stream_eligible,
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
        sb.table("options_universe_snapshots").delete().in_("id", ids_to_delete).execute()
        log.info("universe_store: pruned %d old snapshots", len(ids_to_delete))
    except Exception as e:
        log.warning("universe_store._prune_old_snapshots error (non-fatal): %s", e)
