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
  get_epoch()                   → int   — mutation epoch, incremented per successful save_snapshot()
  get_latest_snapshot_id()      → str   — UUID of most-recent snapshot (startup Step 4 P1 seed)

ROOT CAUSE FIX (2026-04-23) C-005:
  supabase-py v2 does NOT expose .select() after .insert().
  Fix: generate snapshot_id = str(uuid4()) in Python before insert.

ROOT CAUSE FIX (2026-04-23) C-006:
  options_universe_snapshots.provider is NOT NULL with no default.
  Fix: always pass provider="tradier" explicitly.

ROOT CAUSE FIX (2026-04-23) C-007:
  _client() was falling back to the anon key (SUPABASE_KEY) when
  SUPABASE_SERVICE_KEY was not set. The anon key respects RLS and causes
  permission errors on INSERT/UPDATE.
  Fix: raise RuntimeError immediately if SUPABASE_SERVICE_KEY is absent.

ROOT CAUSE FIX (2026-04-30) C-008:
  Migration 010 added open_interest, average_volume, tier columns to
  options_universe_symbols. save_snapshot() and _load_symbols() were not
  aware of these columns, causing silent data loss and None tier values.
  Fix: include all new columns in upsert and SELECT.

ROOT CAUSE FIX (2026-05-05) ING-010:
  load_tier_map() was missing entirely — the tier_map returned to main.py
  lifespan was always {}. Stream workers received no tier assignments.
  Fix: add load_tier_map() which queries options_universe_symbols for the
  most-recent active snapshot and returns {symbol: tier} dict.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from supabase import Client

from core.config import settings

log = logging.getLogger(__name__)

_DEFAULT_MAX_AGE = 24       # hours — fresh snapshot threshold
_UPSERT_BATCH    = 500      # rows per batch for upsert_symbol_quotes
_epoch           = 0        # mutation counter, incremented on each successful save_snapshot()


# ── Public helpers ──────────────────────────────────────────────────────────

def get_epoch() -> int:
    """Return the current universe_store mutation epoch (incremented per save_snapshot success)."""
    return _epoch


async def get_latest_snapshot_id() -> str:
    """
    Return the snapshot UUID of the most-recent active snapshot (max_age=24 h),
    falling back to the absolute latest snapshot in DB regardless of age.

    Used by main.py Step 4 to pass a real UUID to registry.load_from_db()
    so the OCC chain pre-seed reads from options_chain_cache instead of
    triggering a full 4122-ticker Tradier build on every restart.

    Returns "" if no snapshot exists at all.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_get_latest_snapshot_id)


def _sync_get_latest_snapshot_id() -> str:
    try:
        sb     = _client()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        # Try fresh active snapshot first
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
        if rows:
            sid = rows[0]["id"]
            log.info("universe_store.get_latest_snapshot_id: fresh snapshot id=%s", sid)
            return sid
        # Fall back to any snapshot regardless of age
        result = (
            sb.table("options_universe_snapshots")
            .select("id, fetched_at")
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows:
            sid = rows[0]["id"]
            log.info("universe_store.get_latest_snapshot_id: stale fallback id=%s", sid)
            return sid
        log.warning("universe_store.get_latest_snapshot_id: no snapshots found in DB")
        return ""
    except Exception as e:
        log.error("universe_store.get_latest_snapshot_id error: %s", e, exc_info=True)
        return ""


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
            "SUPABASE_SERVICE_KEY is not set. universe_store requires the service role key "
            "to bypass RLS on options_universe_snapshots and options_universe_symbols."
        )
    from supabase import create_client
    return create_client(settings.SUPABASE_URL, service_key)


# ── Public async API ────────────────────────────────────────────────────────

async def load_fresh_snapshot(max_age_hours: int = _DEFAULT_MAX_AGE) -> Optional[list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_fresh_snapshot, max_age_hours)


async def load_any_snapshot() -> Optional[list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_any_snapshot)


async def load_tier_map(max_age_hours: int = _DEFAULT_MAX_AGE) -> dict[str, int]:
    """
    ING-010: Return {symbol: tier} for the most-recent active snapshot.
    Falls back to most-recent stale snapshot if no fresh one exists.
    Returns {} if no snapshot exists at all.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_tier_map, max_age_hours)


async def save_snapshot(
    symbols: list[str],
    source: str = "tradier",
    provider: str = "tradier",
    symbol_rows: Optional[list[dict]] = None,
) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_save_snapshot, symbols, source, provider, symbol_rows
    )


async def upsert_symbol_quotes(
    snapshot_id: str,
    quote_rows: list[dict],
) -> None:
    """
    Upsert symbol quote rows (last_price, volume, open_interest, average_volume, tier)
    into options_universe_symbols for the given snapshot_id.
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_upsert_symbol_quotes, snapshot_id, quote_rows)


# ── Sync implementations ────────────────────────────────────────────────────

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


def _sync_load_tier_map(max_age_hours: int) -> dict[str, int]:
    """ING-010: Load {symbol: tier} for the most-recent snapshot."""
    try:
        sb     = _client()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        # Try fresh first
        result = (
            sb.table("options_universe_snapshots")
            .select("id")
            .eq("is_active", True)
            .gte("fetched_at", cutoff)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            # Fall back to any snapshot
            result = (
                sb.table("options_universe_snapshots")
                .select("id")
                .order("fetched_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = result.data or []
        if not rows:
            log.warning("universe_store.load_tier_map: no snapshot found")
            return {}
        snapshot_id = rows[0]["id"]
        return _load_tier_map_for_snapshot(sb, snapshot_id)
    except Exception as e:
        log.error("universe_store.load_tier_map error: %s", e, exc_info=True)
        return {}


def _load_tier_map_for_snapshot(sb: Client, snapshot_id: str) -> dict[str, int]:
    """Load {symbol: tier} for a specific snapshot, paginating if needed."""
    tier_map: dict[str, int] = {}
    page_size = 1000
    offset    = 0
    while True:
        result = (
            sb.table("options_universe_symbols")
            .select("symbol, tier")
            .eq("snapshot_id", snapshot_id)
            .eq("stream_eligible", True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = result.data or []
        for row in batch:
            sym  = row.get("symbol")
            tier = row.get("tier")
            if sym and tier is not None:
                tier_map[sym] = int(tier)
        if len(batch) < page_size:
            break
        offset += page_size
    log.info(
        "universe_store.load_tier_map: loaded %d tiers from snapshot %s (T1=%d T2=%d T3=%d)",
        len(tier_map), snapshot_id,
        sum(1 for t in tier_map.values() if t == 1),
        sum(1 for t in tier_map.values() if t == 2),
        sum(1 for t in tier_map.values() if t == 3),
    )
    return tier_map


def _load_symbols(sb: Client, snapshot_id: str) -> list[str]:
    """Load stream-eligible symbols for snapshot_id, paginating in chunks of 1000."""
    symbols: list[str] = []
    page_size = 1000
    offset    = 0
    while True:
        result = (
            sb.table("options_universe_symbols")
            .select("symbol")
            .eq("snapshot_id", snapshot_id)
            .eq("stream_eligible", True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = result.data or []
        symbols.extend(row["symbol"] for row in batch if row.get("symbol"))
        if len(batch) < page_size:
            break
        offset += page_size
    log.info(
        "universe_store: loaded %d stream-eligible symbols from snapshot %s",
        len(symbols), snapshot_id,
    )
    return symbols


def _sync_save_snapshot(
    symbols: list[str],
    source: str,
    provider: str,
    symbol_rows: Optional[list[dict]],
) -> bool:
    global _epoch
    try:
        sb          = _client()
        snapshot_id = str(uuid4())

        # Deactivate all existing snapshots
        sb.table("options_universe_snapshots").update({"is_active": False}).neq("id", "00000000-0000-0000-0000-000000000000").execute()

        # Insert new snapshot row
        sb.table("options_universe_snapshots").insert({
            "id":         snapshot_id,
            "is_active":  True,
            "source":     source,
            "provider":   provider,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "symbol_count": len(symbols),
        }).execute()

        # Build symbol rows
        if symbol_rows:
            rows = symbol_rows
        else:
            rows = [{"snapshot_id": snapshot_id, "symbol": s, "stream_eligible": True} for s in symbols]

        # Upsert in batches
        for i in range(0, len(rows), _UPSERT_BATCH):
            batch = rows[i : i + _UPSERT_BATCH]
            sb.table("options_universe_symbols").upsert(
                batch, on_conflict="snapshot_id,symbol"
            ).execute()

        _epoch += 1
        log.info(
            "universe_store: saved snapshot %s (%d symbols, source=%s, epoch=%d)",
            snapshot_id, len(symbols), source, _epoch,
        )
        _prune_old_snapshots(sb, keep=3)
        return True

    except Exception as e:
        log.error("universe_store.save_snapshot error: %s", e, exc_info=True)
        return False


def _sync_upsert_symbol_quotes(snapshot_id: str, quote_rows: list[dict]) -> None:
    try:
        sb = _client()
        total_batches = (len(quote_rows) + _UPSERT_BATCH - 1) // _UPSERT_BATCH

        upsert_rows = [
            {
                "snapshot_id":      snapshot_id,
                "symbol":           r["symbol"],
                "last_price":       r.get("last_price"),
                "volume":           r.get("volume"),
                "open_interest":    r.get("open_interest"),
                "average_volume":   r.get("average_volume"),
                "tier":             r.get("tier"),
                "stream_eligible":  r.get("stream_eligible", True),
            }
            for r in quote_rows
            if r.get("symbol")
        ]

        for batch_num, i in enumerate(range(0, len(upsert_rows), _UPSERT_BATCH), 1):
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
        sb.table("options_universe_symbols").delete().in_("snapshot_id", ids_to_delete).execute()
        sb.table("options_universe_snapshots").delete().in_("id", ids_to_delete).execute()
        log.info("universe_store: pruned %d old snapshots (+ their symbol rows)", len(ids_to_delete))
    except Exception as e:
        log.warning("universe_store._prune_old_snapshots error (non-fatal): %s", e)
