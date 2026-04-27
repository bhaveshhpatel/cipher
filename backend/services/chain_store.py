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
    Load all rows for snapshot_id and return an occ_symbol -> ContractMeta
    mapping.  Returns None on error, empty dict if table is empty.

Design notes
------------
- Uses service_role key (bypasses RLS) for writes.
- Batched in groups of 500 rows to stay within Supabase payload limits.
- Non-fatal: all errors are logged as warnings; callers must not crash on
  failure — the in-memory registry is always the source of truth.
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
    """
    Upsert all ContractMeta rows into options_chain_cache for snapshot_id.
    Returns True on success, False on any error.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _sync_save_chain, snapshot_id, registry_dict
    )


async def load_chain(
    snapshot_id: str,
) -> "Optional[dict[str, ContractMeta]]":
    """
    Load options_chain_cache rows for snapshot_id.
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
    from services.symbol_registry import ContractMeta  # local import avoids circular
    try:
        sb = _client()
        # Paginate — Supabase default page size is 1000; chain can be 50k+ rows
        result: list[dict] = []
        page_size = 1000
        offset    = 0
        while True:
            resp = (
                sb.table(_TABLE)
                .select(
                    "occ_symbol,ticker,contract_type,strike,expiry,dte,open_interest,tier"
                )
                .eq("snapshot_id", snapshot_id)
                .range(offset, offset + page_size - 1)
                .execute()
            )
            page = resp.data or []
            result.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        chain: dict[str, ContractMeta] = {}
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
        log.info(
            "[chain_store] load_chain: loaded %d OCC contracts from snapshot %s",
            len(chain), snapshot_id,
        )
        return chain
    except Exception as exc:
        log.warning("[chain_store] load_chain error (non-fatal): %s", exc, exc_info=True)
        return None
