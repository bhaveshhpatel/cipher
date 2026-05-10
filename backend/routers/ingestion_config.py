"""
routers/ingestion_config.py — Admin API for ingestion floor configuration.

Endpoints:
  GET  /admin/ingestion-config
       Returns all ingestion_config rows cast to their declared Python types.

  PATCH /admin/ingestion-config
        Accepts a JSON object of { key: value } pairs.
        Validates each key is known and each value is within the safe range.
        Upserts to Supabase ingestion_config table.
        Invalidates the in-process cache so the next TTL cycle picks up changes.

Validation ranges (enforced here, not in DB):
  ing.min_dte          int   [1, 5]          — 0DTE must always be rejected
  ing.max_dte          int   [30, 180]
  ing.min_premium.t1   int   [1_000, 500_000]
  ing.min_premium.t2   int   [1_000, 500_000]
  ing.min_premium.t3   int   [1_000, 500_000]
  ing.min_oi           int   [0, 500]
  ing.require_ask_tag  bool  (no range — any bool accepted)

REARCH-002 (2026-05-09)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

from ingestion.processor import invalidate_ingestion_config_cache

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin", "ingestion"])

# ---------------------------------------------------------------------------
# Known keys + validation metadata
# ---------------------------------------------------------------------------

_KEY_META: dict[str, dict] = {
    "ing.min_dte":         {"value_type": "int",  "min": 1,     "max": 5},
    "ing.max_dte":         {"value_type": "int",  "min": 30,    "max": 180},
    "ing.min_premium.t1": {"value_type": "int",  "min": 1_000, "max": 500_000},
    "ing.min_premium.t2": {"value_type": "int",  "min": 1_000, "max": 500_000},
    "ing.min_premium.t3": {"value_type": "int",  "min": 1_000, "max": 500_000},
    "ing.min_oi":          {"value_type": "int",  "min": 0,     "max": 500},
    "ing.require_ask_tag": {"value_type": "bool"},
}


def _cast(value: Any, value_type: str) -> Any:
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    return str(value)


def _to_db_str(value: Any, value_type: str) -> str:
    if value_type == "bool":
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------------------
# GET /admin/ingestion-config
# ---------------------------------------------------------------------------

@router.get("/ingestion-config")
async def get_ingestion_config_endpoint():
    """
    Return all ingestion_config rows as a flat dict { key: typed_value }.
    """
    from services.supabase_client import get_supabase_client
    sb = get_supabase_client()
    resp = sb.table("ingestion_config").select("key,value,value_type,description,updated_at").execute()
    rows = resp.data or []
    result = {}
    for row in rows:
        try:
            result[row["key"]] = {
                "value":       _cast(row["value"], row["value_type"]),
                "value_type":  row["value_type"],
                "description": row.get("description"),
                "updated_at":  row.get("updated_at"),
            }
        except (ValueError, TypeError) as exc:
            log.warning("ingestion_config malformed row %s: %s", row.get("key"), exc)
    return result


# ---------------------------------------------------------------------------
# PATCH /admin/ingestion-config
# ---------------------------------------------------------------------------

class PatchIngestionConfigRequest(BaseModel):
    updates: dict[str, Any]

    @model_validator(mode="after")
    def validate_keys_and_values(self) -> "PatchIngestionConfigRequest":
        errors: list[str] = []
        for key, raw_value in self.updates.items():
            if key not in _KEY_META:
                errors.append(f"Unknown key: '{key}'")
                continue
            meta = _KEY_META[key]
            try:
                cast_value = _cast(raw_value, meta["value_type"])
            except (ValueError, TypeError):
                errors.append(f"'{key}': cannot cast '{raw_value}' to {meta['value_type']}")
                continue
            if meta["value_type"] in ("int", "float") and "min" in meta and "max" in meta:
                if not (meta["min"] <= cast_value <= meta["max"]):
                    errors.append(
                        f"'{key}': value {cast_value} out of range "
                        f"[{meta['min']}, {meta['max']}]"
                    )
        if errors:
            raise ValueError(";".join(errors))
        return self


@router.patch("/ingestion-config")
async def patch_ingestion_config(request: PatchIngestionConfigRequest):
    """
    Update one or more ingestion floor values.
    Returns { updated: [key, ...] } on success.
    Raises 422 for unknown keys or out-of-range values.
    """
    from services.supabase_client import get_supabase_client
    sb = get_supabase_client()

    updated_keys: list[str] = []
    for key, raw_value in request.updates.items():
        meta = _KEY_META[key]
        cast_value = _cast(raw_value, meta["value_type"])
        db_str     = _to_db_str(cast_value, meta["value_type"])
        try:
            sb.table("ingestion_config").upsert(
                {"key": key, "value": db_str, "value_type": meta["value_type"]},
                on_conflict="key",
            ).execute()
            updated_keys.append(key)
            log.info("ingestion_config updated: %s = %s", key, db_str)
        except Exception as exc:
            log.error("ingestion_config DB write failed for key %s: %s", key, exc)
            raise HTTPException(status_code=500, detail=f"DB write failed for '{key}': {exc}") from exc

    # Invalidate cache so the next TTL cycle picks up the change
    invalidate_ingestion_config_cache()

    return {"updated": updated_keys}
