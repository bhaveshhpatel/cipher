"""
services/universe_store.py

Supabase read/write for options universe snapshots.

Tables (must be migrated first):
  options_universe_snapshots  — one row per snapshot, only ONE is_active=true at a time
  options_universe_symbols    — normalized symbol rows per snapshot_id
                                (includes stream_eligible, last_price, volume — migration 002)
                                (includes open_interest, average_volume, tier — migration 010)

Public API:
  load_fresh_snapshot()             → list[str] | None
  load_any_snapshot()               → list[str] | None
  load_tier_map()                   → dict[str, int]        [4A]
  get_latest_snapshot_timestamp()   → datetime              [Issue 2]
  save_snapshot(...)                → bool                  tier_map param added [Issue 6]
  upsert_symbol_quotes(...)         → None

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

Issue 2 fix (2026-04-27):
  get_latest_snapshot_timestamp() returns the fetched_at timestamp of the
  most recent snapshot so _universe_refresh_loop() can anchor its first sleep
  to (24h - elapsed) rather than a flat 24h, preventing a redundant
  _fetch_batch_quotes call when the server restarts near the 24h boundary.

Issue 6 fix (2026-04-27):
  _load_symbols(): add stream_eligible=true filter — loads only ~4,000
  eligible symbols instead of all 5,267 rows, so warm-start stream_symbols
  list is correct without a full pipeline run.

  save_snapshot(): accept optional tier_map param and write real tier values
  per symbol instead of hardcoding tier=3 for all rows.  Preliminary
  tier_map (price+vol only, no OI) is passed by main.py on the cold path
  so the snapshot is never entirely T3 from the start.
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

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _client() -> Client:
    """
    Always use the service role key — it bypasses RLS, which is required
    for all server-side INSERT/UPDATE/DELETE operations.
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
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_load_tier_map)


async def get_latest_snapshot_timestamp() -> datetime:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_get_latest_snapshot_timestamp)


async def save_snapshot(
    symbols: list[str],
    source: str,
    stream_eligible_set: Optional[set[str]] = None,
    tier_map: Optional[dict[str, int]] = None,
) -> bool:
    """
    Persist a universe snapshot.  tier_map (if provided) is used to write
    real per-symbol tier values; defaults to tier=3 for any symbol not in map.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_save_snapshot, symbols, source, stream_eligible_set, tier_map or {}
    )


async def upsert_symbol_quotes(
    quotes: list["SymbolQuote"],
    tier_map: Optional[dict[str, int]] = None,
) -> None:
    if not quotes:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_upsert_symbol_quotes, quotes, tier_map or {})


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
        result = (
            sb.table("options_universe_symbols")
            .select("symbol, tier")
            .eq("snapshot_id", snapshot_id)
            .execute()
        )
        tier_map = {
            r["symbol"]: int(r.get("tier") or 3)
            for r in (result.data or [])
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


def _sync_get_latest_snapshot_timestamp() -> datetime:
    try:
        sb = _client()
        result = (
            sb.table("options_universe_snapshots")
            .select("fetched_at")
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows or not rows[0].get("fetched_at"):
            log.info("universe_store.get_latest_snapshot_timestamp: no snapshots — returning epoch")
            return _EPOCH
        raw = rows[0]["fetched_at"]
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        log.info("universe_store.get_latest_snapshot_timestamp: %s", dt.isoformat())
        return dt
    except Exception as e:
        log.warning(
            "universe_store.get_latest_snapshot_timestamp error (non-fatal): %s", e
        )
        return _EPOCH


def _load_symbols(sb: Client, snapshot_id: str) -> Optional[list[str]]:
    """
    Load stream-eligible symbols for snapshot_id.

    Issue 6 fix: filter stream_eligible=true so warm starts return only the
    ~4,000 eligible symbols instead of the full 5,267-row universe list.
    The full universe is only needed during CBOE+Tradier pipeline runs;
    for streaming we only care about stream-eligible symbols.
    """
    try:
        result = (
            sb.table("options_universe_symbols")
            .select("symbol")
            .eq("snapshot_id", snapshot_id)
            .eq("stream_eligible", True)        # Issue 6: only stream-eligible symbols
            .execute()
        )
        rows    = result.data or []
        symbols = [r["symbol"] for r in rows if r.get("symbol")]
        log.info(
            "universe_store: loaded %d stream-eligible symbols from snapshot %s",
            len(symbols), snapshot_id,
        )
        return symbols if symbols else None
    except Exception as e:
        log.error("universe_store._load_symbols error snapshot=%s: %s", snapshot_id, e, exc_info=True)
        return None


def _sync_save_snapshot(
    symbols: list[str],
    source: str,
    stream_eligible_set: Optional[set[str]] = None,
    tier_map: dict[str, int] = None,          # Issue 6: write real tiers, not hardcoded 3
) -> bool:
    """
    1. Generate snapshot_id locally via uuid4()
    2. Insert snapshot header
    3. Bulk-insert symbols in batches of 500 — includes stream_eligible flag,
       tier from tier_map (default 3 if symbol not in map)
    4. Deactivate all other snapshots
    5. Prune beyond _KEEP_SNAPSHOTS
    """
    if not symbols:
        log.warning("universe_store.save_snapshot: called with empty symbol list — skipping")
        return False
    if tier_map is None:
        tier_map = {}
    try:
        sb          = _client()
        snapshot_id = str(uuid4())

        log.info(
            "universe_store: inserting snapshot id=%s source=%s symbols=%d stream_eligible=%s",
            snapshot_id, source, len(symbols),
            len(stream_eligible_set) if stream_eligible_set is not None else "all",
        )

        sb.table("options_universe_snapshots").insert({
            "id":           snapshot_id,
            "symbol_count": len(symbols),
            "provider":     "tradier",
            "source":       source,
            "is_active":    True,
        }).execute()

        batch_size   = 500
        eligible_set = stream_eligible_set if stream_eligible_set is not None else set(symbols)
        rows = [
            {
                "snapshot_id":     snapshot_id,
                "symbol":          s,
                "stream_eligible": s in eligible_set,
                "tier":            tier_map.get(s, 3),   # Issue 6: real tier, not hardcoded 3
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

        sb.table("options_universe_snapshots").update({"is_active": False}).neq(
            "id", snapshot_id
        ).execute()
        log.info("universe_store: deactivated previous snapshots")

        log.info(
            "universe_store: snapshot SAVED id=%s symbols=%d source=%s tier_map_size=%d",
            snapshot_id, len(symbols), source, len(tier_map),
        )

        _prune_old_snapshots(sb, keep=_KEEP_SNAPSHOTS)
        return True

    except Exception as e:
        log.error("universe_store.save_snapshot error: %s", e, exc_info=True)
        return False


def _sync_upsert_symbol_quotes(quotes: list, tier_map: dict) -> None:
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
        sb.table("options_universe_snapshots").delete().in_("id", ids_to_delete).execute()
        log.info("universe_store: pruned %d old snapshots", len(ids_to_delete))
    except Exception as e:
        log.warning("universe_store._prune_old_snapshots error (non-fatal): %s", e)
