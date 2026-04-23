"""
services/universe_store.py

Supabase read/write for options universe snapshots.

Tables (must be migrated first):
  options_universe_snapshots  — one row per snapshot, only ONE is_active=true at a time
  options_universe_symbols    — normalized symbol rows per snapshot_id
                                (includes stream_eligible column — migration 002)

Public API:
  load_fresh_snapshot()  → list[str] | None
  load_any_snapshot()    → list[str] | None
  save_snapshot(symbols, source, stream_eligible_set)  → bool

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
from typing import Optional
from uuid import uuid4

from supabase import create_client, Client
from config import settings

log = logging.getLogger("universe_store")

_KEEP_SNAPSHOTS  = 7
_DEFAULT_MAX_AGE = 24   # hours


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
        batch_size    = 500
        eligible_set  = stream_eligible_set if stream_eligible_set is not None else set(symbols)
        rows          = [
            {
                "snapshot_id":    snapshot_id,
                "symbol":         s,
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
