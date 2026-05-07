"""
services/gate_config_store.py — ING-010: Tier-aware ingestion gate control plane.

Design
------
- Singleton GateConfigStore loaded at startup from `gate_configs` Supabase table.
- All gate value reads are O(1) in-memory dict lookups — zero DB calls on the
  tick hot path.
- update() writes to DB atomically, persists a full audit row, then refreshes
  the in-memory dict under a threading.Lock.
- Unknown tiers default safely to T3 (most conservative floor).
- Bounds validation rejects unsafe values (e.g. min_premium=0) before any write.

Gates managed
-------------
  min_premium          — parser belowminpremium floor ($)
  dte_floor_multiplier — DTE-adjusted accumulator floor scale factor
  require_oi           — per-tier OI gate boolean (stored as 0/1)
  debounce_ms          — signal debounce window (ms)
  dedup_window_ms      — dedup cache window (ms)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("gate_config_store")

# ---------------------------------------------------------------------------
# Safe hardcoded fallbacks — used when DB is unavailable or row is missing.
# Keyed by (gate_name, tier).  tier is int 1/2/3.
# ---------------------------------------------------------------------------
_FALLBACK: dict[tuple[str, int], Any] = {
    ("min_premium",          1): 25_000,
    ("min_premium",          2): 15_000,
    ("min_premium",          3): 10_000,
    ("dte_floor_multiplier", 1): 1.5,
    ("dte_floor_multiplier", 2): 1.0,
    ("dte_floor_multiplier", 3): 0.75,
    ("require_oi",           1): False,
    ("require_oi",           2): False,
    ("require_oi",           3): False,
    ("debounce_ms",          1): 30_000,
    ("debounce_ms",          2): 60_000,
    ("debounce_ms",          3): 120_000,
    ("dedup_window_ms",      1): 5_000,
    ("dedup_window_ms",      2): 5_000,
    ("dedup_window_ms",      3): 5_000,
}

# Validation bounds for each gate (min_val, max_val inclusive)
_BOUNDS: dict[str, tuple[Any, Any]] = {
    "min_premium":          (1_000,  500_000),
    "dte_floor_multiplier": (0.1,    5.0),
    "require_oi":           (0,      1),
    "debounce_ms":          (1_000,  600_000),
    "dedup_window_ms":      (500,    60_000),
}

_SAFE_DEFAULT_TIER = 3
_VALID_TIERS = frozenset({1, 2, 3})


class GateConfigStore:
    """
    Thread-safe in-memory gate configuration store.

    Usage
    -----
    Instantiate once at app startup, call await store.load() to populate
    from DB. Pass the singleton to any service that needs gate values.

    All per-tick reads call store.get(gate_name, tier) — O(1) dict lookup,
    no locks, no DB I/O.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, int], Any] = dict(_FALLBACK)
        self._lock = threading.Lock()
        self._epoch: int = 0
        self._loaded: bool = False

        self._supabase_url: Optional[str] = os.environ.get("SUPABASE_URL")
        self._supabase_key: Optional[str] = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_SERVICE_KEY")
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "apikey":        self._supabase_key or "",
            "Authorization": f"Bearer {self._supabase_key or ''}",
            "Content-Type":  "application/json",
        }

    @staticmethod
    def _cast(gate_name: str, raw: Any) -> Any:
        """Cast raw DB value to correct Python type for this gate."""
        if gate_name == "require_oi":
            return bool(int(float(raw)))
        if gate_name in ("min_premium", "debounce_ms", "dedup_window_ms"):
            return int(float(raw))
        if gate_name == "dte_floor_multiplier":
            return float(raw)
        return raw

    # ------------------------------------------------------------------
    # Startup load
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """
        Load all gate_configs rows from DB into memory.
        Falls back to _FALLBACK silently on any DB error.
        Safe to call multiple times (idempotent).
        """
        if not self._supabase_url or not self._supabase_key:
            log.warning("[gate_config_store] Supabase not configured — using fallback defaults")
            self._loaded = True
            return

        url = (
            f"{self._supabase_url}/rest/v1/gate_configs"
            "?select=gate_name,tier,value&is_active=eq.true"
        )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                rows = resp.json()
                new_data: dict[tuple[str, int], Any] = dict(_FALLBACK)
                loaded_count = 0
                for row in rows:
                    gate = row.get("gate_name", "")
                    tier = int(row.get("tier", 0))
                    val  = row.get("value")
                    if gate and tier in _VALID_TIERS and val is not None:
                        new_data[(gate, tier)] = self._cast(gate, val)
                        loaded_count += 1
                with self._lock:
                    self._data = new_data
                    self._epoch += 1
                self._loaded = True
                log.info(
                    "[gate_config_store] Loaded %d gate config rows (epoch %d)",
                    loaded_count, self._epoch,
                )
                return
            log.warning(
                "[gate_config_store] DB load failed HTTP %d — using fallback defaults",
                resp.status_code,
            )
        except Exception as exc:
            log.warning("[gate_config_store] DB load error: %s — using fallback defaults", exc)
        self._loaded = True

    # ------------------------------------------------------------------
    # Per-tick read (hot path — lock-free)
    # ------------------------------------------------------------------

    def get(self, gate_name: str, tier: int) -> Any:
        """
        Return the configured value for (gate_name, tier).
        Falls back to T3 default for unknown tiers.
        Never raises — always returns a safe value.
        """
        safe_tier = tier if tier in _VALID_TIERS else _SAFE_DEFAULT_TIER
        return self._data.get(
            (gate_name, safe_tier),
            _FALLBACK.get((gate_name, _SAFE_DEFAULT_TIER)),
        )

    @property
    def epoch(self) -> int:
        """Monotonically incrementing counter — callers can detect config changes."""
        return self._epoch

    # ------------------------------------------------------------------
    # Admin read — full snapshot
    # ------------------------------------------------------------------

    def get_all(self) -> list[dict]:
        """
        Return all current gate config values with bounds metadata.
        Used by GET /api/admin/gate-config.
        """
        rows = []
        seen: set[tuple[str, int]] = set()
        for (gate, tier), value in sorted(self._data.items()):
            seen.add((gate, tier))
            lo, hi = _BOUNDS.get(gate, (None, None))
            rows.append({
                "gate_name":  gate,
                "tier":       tier,
                "value":      value,
                "min_bound":  lo,
                "max_bound":  hi,
            })
        # Include fallback rows that may not yet be in DB
        for (gate, tier), value in sorted(_FALLBACK.items()):
            if (gate, tier) not in seen:
                lo, hi = _BOUNDS.get(gate, (None, None))
                rows.append({
                    "gate_name":  gate,
                    "tier":       tier,
                    "value":      value,
                    "min_bound":  lo,
                    "max_bound":  hi,
                    "_source":    "fallback",
                })
        return rows

    # ------------------------------------------------------------------
    # Admin write — hot-reload update
    # ------------------------------------------------------------------

    async def update(
        self,
        gate_name: str,
        tier: int,
        new_value: Any,
        updated_by: str = "admin",
        reason: str = "",
        confirm_market_hours: bool = False,
    ) -> dict:
        """
        Validate → persist to DB → update in-memory dict → return result.

        Returns dict with keys: gate_name, tier, old_value, new_value,
        propagated_at, epoch.

        Raises ValueError on validation failure.
        Raises RuntimeError on DB write failure.
        """
        # --- Tier validation ---
        if tier not in _VALID_TIERS:
            raise ValueError(f"Invalid tier {tier!r}. Must be one of {sorted(_VALID_TIERS)}.")

        # --- Gate existence check ---
        if gate_name not in _BOUNDS:
            raise ValueError(
                f"Unknown gate {gate_name!r}. Valid gates: {sorted(_BOUNDS)}"
            )

        # --- Bounds validation ---
        lo, hi = _BOUNDS[gate_name]
        cast_value = self._cast(gate_name, new_value)
        numeric_check = int(cast_value) if gate_name == "require_oi" else cast_value
        if not (lo <= numeric_check <= hi):
            raise ValueError(
                f"Value {new_value!r} for gate '{gate_name}' is outside allowed "
                f"bounds [{lo}, {hi}]."
            )

        # --- Market-hours guard ---
        import datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        is_market_hours = (
            now_utc.weekday() < 5
            and datetime.time(13, 30) <= now_utc.time() <= datetime.time(20, 0)
        )
        if is_market_hours and not confirm_market_hours:
            raise ValueError(
                "Market is currently open. Pass confirm_market_hours=true to proceed."
            )

        old_value = self.get(gate_name, tier)

        if not self._supabase_url or not self._supabase_key:
            # No DB — update in-memory only (dev/test mode)
            with self._lock:
                self._data[(gate_name, tier)] = cast_value
                self._epoch += 1
            return {
                "gate_name":      gate_name,
                "tier":           tier,
                "old_value":      old_value,
                "new_value":      cast_value,
                "propagated_at":  now_utc.isoformat(),
                "epoch":          self._epoch,
                "_note":          "no-db mode — in-memory only",
            }

        # --- DB PATCH ---
        patch_url = (
            f"{self._supabase_url}/rest/v1/gate_configs"
            f"?gate_name=eq.{gate_name}&tier=eq.{tier}"
        )
        patch_payload = {
            "value":      str(cast_value),
            "updated_by": updated_by,
            "updated_at": now_utc.isoformat(),
        }
        headers_patch = {**self._headers(), "Prefer": "return=minimal"}

        # --- DB INSERT audit row ---
        audit_url = f"{self._supabase_url}/rest/v1/gate_config_audit"
        audit_payload = {
            "gate_name":      gate_name,
            "tier":           tier,
            "old_value":      str(old_value),
            "new_value":      str(cast_value),
            "updated_by":     updated_by,
            "reason":         reason,
            "changed_at":     now_utc.isoformat(),
            "was_market_hours": is_market_hours,
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                patch_resp = await client.patch(
                    patch_url, headers=headers_patch, json=patch_payload
                )
                audit_resp = await client.post(
                    audit_url, headers=self._headers(), json=audit_payload
                )

            if patch_resp.status_code not in (200, 204):
                raise RuntimeError(
                    f"DB PATCH failed for ({gate_name}, tier={tier}): "
                    f"HTTP {patch_resp.status_code} {patch_resp.text[:200]}"
                )
            if audit_resp.status_code not in (200, 201):
                log.warning(
                    "[gate_config_store] Audit insert failed HTTP %d — gate was updated",
                    audit_resp.status_code,
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"DB write error for ({gate_name}, tier={tier}): {exc}") from exc

        # --- In-memory update (atomic under lock) ---
        with self._lock:
            self._data[(gate_name, tier)] = cast_value
            self._epoch += 1

        propagated_at = time.monotonic()
        log.info(
            "[gate_config_store] Updated %s tier=%d: %r → %r by %s (epoch %d)",
            gate_name, tier, old_value, cast_value, updated_by, self._epoch,
        )

        return {
            "gate_name":      gate_name,
            "tier":           tier,
            "old_value":      old_value,
            "new_value":      cast_value,
            "propagated_at":  now_utc.isoformat(),
            "epoch":          self._epoch,
        }


# ---------------------------------------------------------------------------
# Module-level singleton — import and use this everywhere.
# ---------------------------------------------------------------------------
gate_config_store = GateConfigStore()
