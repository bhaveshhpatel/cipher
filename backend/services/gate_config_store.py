"""
services/gate_config_store.py — in-memory gate configuration store  [ING-010]

Provides a thin, thread-safe in-memory cache over the gate_configs table.
All hot-path gate reads (accumulator, parser, signal engine) go through
  store.get(gate_name, tier) -> float
which returns in O(1) without a DB round-trip.

Updates are committed to the DB first, then the in-memory cache is
patched atomically via store.update() so there is never a window where
the DB has a new value but memory still has the old one.

Return contract:
  store.get(gate_name, tier) always returns float.
  Returns 0.0 for unknown gates or missing tiers (never None).
"""
from __future__ import annotations

import datetime
import logging
import threading
from typing import Any

import httpx

log = logging.getLogger("gate_config_store")

# ---------------------------------------------------------------------------
# Gate name → alias resolution
# ---------------------------------------------------------------------------
_ALIAS_MAP: dict[str, str] = {
    "debounce_ms": "signal_debounce_ms",
}

# Valid gate names (canonical DB names).
_VALID_GATES: frozenset[str] = frozenset({
    "min_premium",
    "dte_floor_multiplier",
    "dedup_window_ms",
    "require_oi",
    "signal_debounce_ms",
    "signal_min_premium",
    "exclude_indices",
})

# Valid tier integers. Exported so tests can iterate without hardcoding.
_VALID_TIERS: frozenset[int] = frozenset({1, 2, 3})

# The tier used as the safe fallback when an unknown tier is requested.
_SAFE_DEFAULT_TIER: int = 3

# ---------------------------------------------------------------------------
# Static bounds — gate_name → (min, max, cast)
#
# 3-tuple: test_gates_tiered.py unpacks as  lo, hi, cast = _BOUNDS[gate]
# update() uses raw[0]/raw[1] indexing so the cast element is harmless there.
# test_gate_config_store.py unpacks as      lo, hi, _ = _BOUNDS[gate]
# ---------------------------------------------------------------------------
_BOUNDS: dict[str, tuple[float, float, type]] = {
    "min_premium":          (1_000.0,  500_000.0, float),
    "dte_floor_multiplier": (0.1,      5.0,       float),
    "dedup_window_ms":      (500.0,    60_000.0,  float),
    "require_oi":           (0.0,      1.0,       float),
    "signal_debounce_ms":   (1_000.0,  600_000.0, float),
    "signal_min_premium":   (1_000.0,  500_000.0, float),
    "exclude_indices":      (0.0,      1.0,       float),
}

# ---------------------------------------------------------------------------
# Hard-coded defaults
#
# _DEFAULTS  — nested {gate: {tier: value}}
#              Consumed by:
#                test_gate_hotreload.py B1:  for gate, tiers in _DEFAULTS.items()
#                test_ing011_*.py:           gate in _DEFAULTS; _DEFAULTS[gate][tier]
#              Also used internally to seed GateConfigStore._cache.
#
# _FALLBACK  — flat {(gate, tier): value}
#              Consumed by:
#                test_gates_tiered.py:      (gate, tier) in _FALLBACK
#                test_gate_config_store.py: for (gate, tier), val in _FALLBACK.items()
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, dict[int, float]] = {
    "min_premium": {
        1: 25_000.0,
        2: 15_000.0,
        3: 10_000.0,
    },
    "dte_floor_multiplier": {
        1: 1.5,
        2: 1.0,
        3: 0.75,
    },
    "dedup_window_ms": {
        1: 5_000.0,
        2: 5_000.0,
        3: 5_000.0,
    },
    "require_oi": {
        1: 0.0,
        2: 0.0,
        3: 0.0,
    },
    "signal_debounce_ms": {
        1: 30_000.0,
        2: 60_000.0,
        3: 120_000.0,
    },
    "signal_min_premium": {
        1: 75_000.0,
        2: 50_000.0,
        3: 25_000.0,
    },
    "exclude_indices": {
        1: 1.0,
        2: 1.0,
        3: 1.0,
    },
}

# Flat alias — exported for test_gates_tiered.py and test_gate_config_store.py.
_FALLBACK: dict[tuple[str, int], float] = {
    (gate, tier): value
    for gate, tiers in _DEFAULTS.items()
    for tier, value in tiers.items()
}


def _is_market_open() -> bool:
    """Return True if the US equity market is currently open (9:30–16:00 ET, Mon–Fri)."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now = datetime.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t  = datetime.time(9, 30)
    close_t = datetime.time(16, 0)
    return open_t <= now.time() < close_t


class GateConfigStore:
    """
    Thread-safe in-memory cache for gate_configs rows.

    Lifecycle
    ---------
    1. Call ``await store.load()`` once at startup to populate from DB.
    2. All subsequent reads via ``store.get(gate_name, tier)`` are O(1),
       lock-free (reading a dict is GIL-safe for CPython).
    3. Writes go through ``await store.update(...)`` which commits to
       the DB first, then patches the in-memory cache under a lock.
    """

    def __init__(self) -> None:
        # Seed cache from _DEFAULTS (nested form).
        self._cache: dict[str, dict[int, float]] = {
            gate: dict(tiers) for gate, tiers in _DEFAULTS.items()
        }
        self._bounds_cache: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._supabase_url: str = ""
        self._supabase_key: str = ""
        self.epoch: int = 0

    def _resolve_credentials(self) -> tuple[str, str]:
        url = self._supabase_url or ""
        key = self._supabase_key or ""
        if not url or not key:
            from config import settings
            url = getattr(settings, "SUPABASE_URL", "") or ""
            key = getattr(settings, "SUPABASE_SERVICE_KEY", "") or ""
        return url, key

    @staticmethod
    def _resolve_alias(gate_name: str) -> str:
        return _ALIAS_MAP.get(gate_name, gate_name)

    def get(self, gate_name: str, tier: int) -> float:
        """
        Return the current value for ``gate_name`` × ``tier``.
        Always returns float — 0.0 for unknown gates or missing tiers.
        """
        gate_name = self._resolve_alias(gate_name)
        if gate_name not in _VALID_GATES:
            return 0.0
        if tier not in _VALID_TIERS:
            tier = _SAFE_DEFAULT_TIER
        val = self._cache.get(gate_name, {}).get(tier)
        return float(val) if val is not None else 0.0

    async def load(self) -> None:
        """
        Bulk-load all rows from gate_configs into the in-memory cache.
        Falls back silently when no credentials are available.
        Epoch is incremented on any successful HTTP 2xx response.
        """
        url, key = self._resolve_credentials()
        if not url or not key:
            log.warning("[gate_config] No Supabase URL/key — using hardcoded defaults")
            return

        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }
        async with httpx.AsyncClient(base_url=url, headers=headers) as client:
            resp = await client.get("/rest/v1/gate_configs", params={"select": "*"})
            resp.raise_for_status()
            rows: list[dict] = resp.json()

        new_cache: dict[str, dict[int, float]] = {
            gate: dict(tiers) for gate, tiers in _DEFAULTS.items()
        }
        new_bounds: dict[str, tuple[float, float]] = {}
        for row in rows:
            name = row.get("gate_name", "")
            t    = row.get("tier")
            val  = row.get("value")
            lo   = row.get("min_value", 0.0)
            hi   = row.get("max_value", float("inf"))
            if name and t is not None and val is not None:
                new_cache.setdefault(name, {})[int(t)] = float(val)
                new_bounds[name] = (float(lo), float(hi))

        with self._lock:
            self._cache = new_cache
            self._bounds_cache = new_bounds
            self.epoch += 1

        log.info("[gate_config] Loaded %d rows from DB (epoch=%d)", len(rows), self.epoch)

    async def update(
        self,
        gate_name: str,
        tier: int,
        value: float,
        updated_by: str = "system",
        reason: str | None = None,
        confirm_market_hours: bool = False,
    ) -> dict[str, Any]:
        """
        Commit a gate value change to the DB and hot-patch the cache.
        """
        gate_name = self._resolve_alias(gate_name)
        if gate_name not in _VALID_GATES:
            raise ValueError(f"Unknown gate: {gate_name!r}")
        if tier not in _VALID_TIERS:
            raise ValueError(f"Invalid tier {tier!r} — must be one of {sorted(_VALID_TIERS)}")

        bounds = self._bounds_cache.get(gate_name)
        if bounds is None:
            raw = _BOUNDS.get(gate_name)
            lo, hi = (raw[0], raw[1]) if raw is not None else (float("-inf"), float("inf"))
        else:
            lo, hi = bounds[0], bounds[1]

        if not (lo <= float(value) <= hi):
            raise ValueError(
                f"{gate_name!r} value {value} is outside allowed bounds [{lo}, {hi}]"
            )

        if _is_market_open() and not confirm_market_hours:
            raise ValueError(
                "Market is currently open — pass confirm_market_hours=True to force update"
            )

        old_value = self.get(gate_name, tier)

        url, key = self._resolve_credentials()
        if not url or not key:
            with self._lock:
                self._cache.setdefault(gate_name, {})[tier] = float(value)
                self.epoch += 1
            log.info(
                "[gate_config] no-DB mode: %s[T%d] = %s (epoch=%d)",
                gate_name, tier, value, self.epoch,
            )
            return {
                "gate_name": gate_name,
                "tier": tier,
                "old_value": old_value,
                "new_value": float(value),
                "_note": "no-db mode — in-memory only",
            }

        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        async with httpx.AsyncClient(base_url=url, headers=headers) as client:
            patch_resp = await client.patch(
                "/rest/v1/gate_configs",
                params={"gate_name": f"eq.{gate_name}", "tier": f"eq.{tier}"},
                json={"value": float(value), "updated_by": updated_by},
            )
            if patch_resp.status_code not in (200, 204):
                raise RuntimeError(f"DB PATCH failed: HTTP {patch_resp.status_code}")

            try:
                await client.post(
                    "/rest/v1/gate_config_audit",
                    json={
                        "gate_name":  gate_name,
                        "tier":       tier,
                        "old_value":  old_value,
                        "new_value":  float(value),
                        "changed_by": updated_by,
                        "reason":     reason,
                        "changed_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                    },
                )
            except Exception as audit_exc:  # noqa: BLE001
                log.warning("[gate_config] audit write failed (non-fatal): %s", audit_exc)

        with self._lock:
            self._cache.setdefault(gate_name, {})[tier] = float(value)
            self.epoch += 1

        log.info(
            "[gate_config] %s[T%d] updated: %s -> %s by %s (epoch=%d)",
            gate_name, tier, old_value, value, updated_by, self.epoch,
        )
        return {
            "gate_name": gate_name,
            "tier": tier,
            "old_value": old_value,
            "new_value": float(value),
        }

    @staticmethod
    def assert_store_epoch_parity(
        gate_epoch: int,
        chain_epoch: int,
        universe_epoch: int,
    ) -> None:
        if chain_epoch != 0 and chain_epoch > gate_epoch:
            raise AssertionError(
                f"chain_store epoch {chain_epoch} is ahead of gate_store epoch "
                f"{gate_epoch} — gate store must be loaded first"
            )
        if universe_epoch != 0 and universe_epoch > gate_epoch:
            raise AssertionError(
                f"universe_store epoch {universe_epoch} is ahead of gate_store epoch "
                f"{gate_epoch} — gate store must be loaded first"
            )


# Module-level singleton — imported by all consumers.
store = GateConfigStore()
gate_config_store = store   # alias used by main.py lifespan
