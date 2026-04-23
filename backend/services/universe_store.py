"""
services/universe_store.py

Supabase read/write for options universe snapshots.

Tables (must be migrated first):
  options_universe_snapshots  — one row per snapshot, only ONE is_active=true at a time
  options_universe_symbols    — normalized symbol rows per snapshot_id

Public API:
  load_fresh_snapshot()  → list[str] | None
      Returns symbols from the most recent active snapshot younger than max_age_hours.
      Returns None if no fresh snapshot exists.

  load_any_snapshot()  → list[str] | None
      Returns symbols from the most recent snapshot regardless of age.
      Used as stale fallback when Tradier is down.

  save_snapshot(symbols, source)  → bool
      Persists a new snapshot, deactivates the old one, prunes snapshots older than 7.
      Returns True on success.

IMPORTANT: All public functions are async-safe.
  The underlying Supabase client is synchronous (blocking I/O). To avoid
  starving the FastAPI event loop (which would cause silent None returns
  and fall-through to seed fallback), every public function runs the
  blocking work inside asyncio.get_event_loop().run_in_executor(None, ...).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from supabase import create_client, Client
from config import settings

log = logging.getLogger("universe_store")

_KEEP_SNAPSHOTS  = 7    # number of most-recent snapshots to retain
_DEFAULT_MAX_AGE = 24   # hours — snapshot freshness window


def _client() -> Client:
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY,
    )


# ---------------------------------------------------------------------------
# Async wrappers — run blocking Supabase I/O off the event loop
# ---------------------------------------------------------------------------

async def load_fresh_snapshot(max_age_hours: int = _DEFAULT_MAX_AGE) -> Optional[list[str]]:
    """
    Return symbols from the active snapshot if it is younger than max_age_hours.
    Returns None if no fresh active snapshot exists.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_fresh_snapshot, max_age_hours)


async def load_any_snapshot() -> Optional[list[str]]:
    """
    Return symbols from the most recent snapshot regardless of age.
    Stale fallback when Tradier is unavailable.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_any_snapshot)


async def save_snapshot(symbols: list[str], source: str) -> bool:
    """
    Persist a new snapshot, deactivate the old one, prune old snapshots.
    Returns True on success.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_save_snapshot, symbols, source)


# ---------------------------------------------------------------------------
# Synchronous implementations (called via run_in_executor)
# ---------------------------------------------------------------------------

def _sync_load_fresh_snapshot(max_age_hours: int) -> Optional[list[str]]:
    try:
        sb = _client()
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
        rows = result.data or []
        symbols = [r["symbol"] for r in rows if r.get("symbol")]
        log.info("universe_store: loaded %d symbols from snapshot %s", len(symbols), snapshot_id)
        return symbols if symbols else None
    except Exception as e:
        log.error("universe_store._load_symbols error snapshot=%s: %s", snapshot_id, e, exc_info=True)
        return None


def _sync_save_snapshot(symbols: list[str], source: str) -> bool:
    """
    1. Insert new snapshot row (is_active=True)
    2. Bulk-insert all symbols in batches of 500
    3. Deactivate all other snapshots
    4. Prune snapshots beyond _KEEP_SNAPSHOTS
    """
    if not symbols:
        log.warning("universe_store.save_snapshot: called with empty symbol list — skipping")
        return False
    try:
        sb = _client()
        log.info("universe_store: inserting snapshot header (source=%s, symbols=%d)", source, len(symbols))

        # 1. Insert snapshot header
        snap_result = (
            sb.table("options_universe_snapshots")
            .insert({
                "symbol_count": len(symbols),
                "source":       source,
                "is_active":    True,
            })
            .execute()
        )
        snap_rows = snap_result.data or []
        if not snap_rows:
            log.error("universe_store.save_snapshot: insert returned no rows — check RLS/table perms")
            return False
        snapshot_id = snap_rows[0]["id"]
        log.info("universe_store: snapshot header inserted id=%s", snapshot_id)

        # 2. Bulk insert symbols in batches of 500
        batch_size = 500
        rows = [{"snapshot_id": snapshot_id, "symbol": s} for s in symbols]
        total_batches = (len(rows) + batch_size - 1) // batch_size
        for i in range(0, len(rows), batch_size):
            batch_num = i // batch_size + 1
            sb.table("options_universe_symbols").insert(rows[i:i + batch_size]).execute()
            log.info(
                "universe_store: inserted symbol batch %d/%d (%d symbols)",
                batch_num, total_batches, len(rows[i:i + batch_size]),
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
