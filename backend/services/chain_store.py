"""
services/chain_store.py

Persists and loads the OCC symbol registry (ContractMeta rows) to/from
the options_chain_cache Supabase table (migration 012).

Public API
----------
save_chain(snapshot_id, registry_dict)  -> bool
    Batch-upsert all ContractMeta rows for the current active snapshot.
    All batches are dispatched CONCURRENTLY via asyncio.gather (C-1 fix).
    Called after every SymbolRegistry.build().

load_chain(snapshot_id, max_age_hours)  -> dict[str, ContractMeta] | None
    Load all rows for snapshot_id.
    Falls back to the most-recent snapshot in options_chain_cache that
    has rows AND is within max_age_hours (default 24) (C-2 fix).
    Returns None on DB error, empty dict if no fresh chains exist.

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
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

from supabase import create_client, Client
from config import settings

if TYPE_CHECKING:
    from services.symbol_registry import ContractMeta

log = logging.getLogger("chain_store")

_TABLE      = "options_chain_cache"
_BATCH_SIZE = 500
_PAGE_SIZE  = 1000
_DEFAULT_MAX_AGE_HOURS = 24

# ---------------------------------------------------------------------------
# Epoch counter — incremented on every successful save_chain().
# Starts at 0 (pre-first-save); get_epoch() exposes it publicly.
# ---------------------------------------------------------------------------
_epoch: int = 0


def get_epoch() -> int:
    """Return the current chain_store mutation epoch (incremented per save_chain success)."""
    return _epoch


def _client() -> Client:
    key = settings.SUPABASE_SERVICE_KEY
    if not key:
        raise RuntimeError(
            "[chain_store] SUPABASE_SERVICE_KEY not set — "
            "cannot read/write options_chain_cache."
        )
    return create_client(settings.SUPABASE_URL, key)


async def save_chain(
    snapshot_id: str,
    registry_dict: "dict[str, ContractMeta]",
) -> bool:
    """
    C-1 FIX: Persist all ContractMeta rows concurrently.

    Splits rows into _BATCH_SIZE chunks and dispatches all upsert coroutines
    concurrently via asyncio.gather(). Each batch runs in its own
    run_in_executor call so the event loop is never blocked by a single
    sequential batch write.

    Wall time improvement: ~5.8s (sequential) → ~300ms (concurrent).

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
            "tier":          int(m.tier),
        }
        for occ, m in registry_dict.items()
    ]
    total   = len(rows)
    batches = [rows[i : i + _BATCH_SIZE] for i in range(0, total, _BATCH_SIZE)]
    n_batches = len(batches)

    async def _upsert_batch(batch: list, batch_num: int) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _client().table(_TABLE).upsert(
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
                "occ_symbol,ticker,contract_type,strike,expiry,dte,open_interest,tier"
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
        chain[occ] = ContractMeta(
            ticker        = row["ticker"],
            strike        = float(row["strike"]),
            expiry        = row["expiry"],
            contract_type = row["contract_type"],
            dte           = int(row["dte"]),
            open_interest = int(row["open_interest"]),
            tier          = int(row.get("tier") or 3),
        )
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
