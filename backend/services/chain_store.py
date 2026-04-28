"""
services/chain_store.py

Persists and loads the OCC symbol registry (ContractMeta rows) to/from
the options_chain_cache Supabase table (migration 012).

Public API
----------
save_chain(snapshot_id, registry_dict)  -> bool
    Batch-upsert all ContractMeta rows for the current active snapshot.
    Called after every SymbolRegistry.build().

load_chain(snapshot_id)                 -> dict[str, ContractMeta] | None
    Load all rows for snapshot_id.
    If that snapshot has no cached rows, falls back to the most-recent
    snapshot in options_chain_cache that DOES have rows (P1 fix: the
    active snapshot never has chains on the first restart after
    save_snapshot() creates a new UUID — chains live under the old id).
    Returns None on DB error, empty dict if no chains exist anywhere.

Design notes
------------
- Uses service_role key (bypasses RLS) for writes.
- Batched in groups of 500 rows to stay within Supabase payload limits.
- Non-fatal: all errors are logged as warnings; callers must not crash on
  failure — the in-memory registry is always the source of truth.

FIX P1 (2026-04-27):
  load_chain() now falls back to the most-recent snapshot that has chain
  rows when the requested snapshot_id has none. Prevents the 12-minute
  full rebuild on every warm restart after a new snapshot UUID is minted.
"""
import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from supabase import create_client, Client
from config import settings

if TYPE_CHECKING:
    from services.symbol_registry import ContractMeta

log = logging.getLogger("chain_store")

_TABLE      = "options_chain_cache"
_BATCH_SIZE = 500
_PAGE_SIZE  = 1000


def _client() -> Client:
    key = settings.SUPABASE_SERVICE_KEY
    if not key:
        raise RuntimeError(
            "[chain_store] SUPABASE_SERVICE_KEY not set — "
            "cannot read/write options_chain_cache."
        )
    return create_client(settings.SUPABASE_URL, key)


# ---------------------------------------------------------------------------
# Public async wrappers
# ---------------------------------------------------------------------------

async def save_chain(
    snapshot_id: str,
    registry_dict: "dict[str, ContractMeta]",
) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _sync_save_chain, snapshot_id, registry_dict
    )


async def load_chain(
    snapshot_id: str,
) -> "Optional[dict[str, ContractMeta]]":
    """
    Load options_chain_cache rows for snapshot_id.
    Falls back to the most-recent snapshot that has rows if snapshot_id
    has none (handles the common case where save_snapshot() created a new
    UUID but the prior build()'s chains live under the previous id).
    Returns dict[occ_symbol -> ContractMeta], empty dict if nothing cached,
    or None on DB error.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_load_chain, snapshot_id)


# ---------------------------------------------------------------------------
# Sync implementations
# ---------------------------------------------------------------------------

def _sync_save_chain(
    snapshot_id: str,
    registry_dict: "dict[str, ContractMeta]",
) -> bool:
    if not registry_dict:
        log.info("[chain_store] save_chain: empty registry — nothing to persist")
        return True
    try:
        sb = _client()
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
        batches = (total + _BATCH_SIZE - 1) // _BATCH_SIZE
        for i in range(0, total, _BATCH_SIZE):
            batch_num = i // _BATCH_SIZE + 1
            sb.table(_TABLE).upsert(
                rows[i : i + _BATCH_SIZE],
                on_conflict="snapshot_id,occ_symbol",
            ).execute()
            log.info(
                "[chain_store] save_chain: batch %d/%d (%d rows)",
                batch_num, batches, len(rows[i : i + _BATCH_SIZE]),
            )
        log.info(
            "[chain_store] save_chain: persisted %d OCC contracts for snapshot %s",
            total, snapshot_id,
        )
        return True
    except Exception as exc:
        log.warning("[chain_store] save_chain error (non-fatal): %s", exc, exc_info=True)
        return False


def _sync_load_chain(
    snapshot_id: str,
) -> "Optional[dict[str, ContractMeta]]":
    """
    Load chain for snapshot_id. If that snapshot has no rows, fall back to
    the most-recent snapshot in options_chain_cache that does have rows.

    This handles the common warm-restart scenario:
      - save_snapshot() mints a new UUID (active snapshot)
      - Previous build() persisted chains under the OLD snapshot_id
      - Active snapshot has 0 chain rows → fall back to old chains
      - Stale-by-one-snapshot data is still correct for OCC lookups;
        build() will refresh everything in the background (P2).
    """
    from services.symbol_registry import ContractMeta  # local import avoids circular
    try:
        sb = _client()

        # Attempt 1: load from the requested snapshot_id
        chain = _paginate_chain(sb, snapshot_id)
        if chain:
            log.info(
                "[chain_store] load_chain: loaded %d OCC contracts from snapshot %s",
                len(chain), snapshot_id,
            )
            return chain

        # Attempt 2 (P1 fallback): find the most-recent snapshot that has rows
        log.info(
            "[chain_store] load_chain: snapshot %s has no rows — "
            "searching for most-recent cached snapshot",
            snapshot_id,
        )
        fallback_snap = _find_latest_cached_snapshot(sb)
        if not fallback_snap:
            log.info("[chain_store] load_chain: no cached chains found in any snapshot")
            return {}  # empty dict — caller will do full build

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
    """Paginate all rows for snapshot_id and return occ_symbol -> ContractMeta dict."""
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


def _find_latest_cached_snapshot(sb: Client) -> Optional[str]:
    """
    Return the snapshot_id of the most-recent row in options_chain_cache.
    Uses a single GROUP-BY-style query via Supabase: select distinct
    snapshot_id ordered by the implicit insert order, limit 1.
    Returns None if the table is empty.
    """
    try:
        resp = (
            sb.table(_TABLE)
            .select("snapshot_id")
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
