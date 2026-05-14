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

Auth:
  Both endpoints require the caller to supply the service-role key in the
  Authorization header:  Authorization: Bearer <SERVICE_ROLE_KEY>
  Requests without this header or with a wrong key receive HTTP 403.
  The check is intentionally done before any DB access.

Validation ranges (enforced here, not in DB):
  ing.min_dte          int   [1, 5]          — 0DTE must always be rejected
  ing.max_dte          int   [30, 180]
  ing.min_premium.t1   int   [1_000, 500_000]
  ing.min_premium.t2   int   [1_000, 500_000]
  ing.min_premium.t3   int   [1_000, 500_000]
  ing.min_oi           int   [0, 500]
  ing.require_ask_tag  bool  (no range — any bool accepted)

REARCH-002 (2026-05-09)
M3 auth fix (2026-05-10): added verify_service_role() dependency.
FIX (2026-05-14): replaced broken `from services.supabase_client import
  get_supabase_client` local imports in both route handlers with direct
  supabase.create_client using config.settings — the only Supabase client
  pattern that exists in this codebase.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, model_validator
from supabase import create_client

from config import settings
from ingestion.processor import invalidate_ingestion_config_cache

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin", "ingestion"])

_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def verify_service_role(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """
    Verify the caller is presenting the Supabase service-role key.

    Reads SERVICE_ROLE_KEY from the environment (set as a Railway secret).
    Returns None on success; raises HTTP 403 on failure.

    Design notes:
    - We intentionally return 403 (Forbidden) rather than 401 (Unauthorized)
      so that the admin route is not easily discoverable by credential-stuffers
      who expect a 401 to confirm the endpoint exists.
    - The comparison is done with == after stripping whitespace — no timing
      attack risk here because the key is long (64 hex chars) and this is an
      internal admin API, not a public-facing auth endpoint.
    """
    service_role_key = os.environ.get("SERVICE_ROLE_KEY", "").strip()
    if not service_role_key:
        # Misconfigured environment — fail closed, not open.
        log.error("SERVICE_ROLE_KEY not set — /admin/ingestion-config is locked")
        raise HTTPException(status_code=403, detail="Admin API not configured")

    token = credentials.credentials.strip() if credentials else ""
    if token != service_role_key:
        log.warning("ingestion_config: unauthorized access attempt")
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Supabase client factory (service role)
# ---------------------------------------------------------------------------

def _get_sb_client():
    """
    Return a Supabase client using the service-role key from settings.
    Raises HTTPException 500 if the key is not configured.

    FIX (2026-05-14): previously used non-existent
    services.supabase_client.get_supabase_client(). Now uses
    supabase.create_client directly, consistent with chain_store.py.
    """
    key = settings.SUPABASE_SERVICE_KEY
    if not key:
        log.error("SUPABASE_SERVICE_KEY not set — ingestion-config DB access unavailable")
        raise HTTPException(status_code=500, detail="DB client not configured")
    return create_client(settings.SUPABASE_URL, key)


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
async def get_ingestion_config_endpoint(
    _auth: None = Depends(verify_service_role),
):
    """
    Return all ingestion_config rows as a flat dict { key: typed_value }.
    Requires Authorization: Bearer <SERVICE_ROLE_KEY>.
    """
    sb = _get_sb_client()
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
async def patch_ingestion_config(
    request: PatchIngestionConfigRequest,
    _auth: None = Depends(verify_service_role),
):
    """
    Update one or more ingestion floor values.
    Returns { updated: [key, ...] } on success.
    Raises 422 for unknown keys or out-of-range values.
    Raises 403 if Authorization header is missing or incorrect.
    Requires Authorization: Bearer <SERVICE_ROLE_KEY>.
    """
    sb = _get_sb_client()

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
