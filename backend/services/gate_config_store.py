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

import asyncio
import logging
import threading
from datetime import datetime, time, timezone
from typing import Any

import httpx  # top-level so tests can mock services.gate_config_store.httpx

log = logging.getLogger("gate_config_store")

# ---------------------------------------------------------------------------
# Gate name → alias resolution
# The DB stores the canonical name; code may use either spelling.
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
# Static bounds — used for update() validation when the DB has not yet
# surfaced min_value/max_value columns (pre-load or no-DB mode).
# Keyed by canonical gate name → (min, max, cast).
# After load(), _bounds_cache on the instance may override these per-row.
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
# Hard-coded defaults — nested dict: gate_name -> tier -> value
# Used when the DB is unreachable or a row is missing.
# These MUST match the seed rows in 013_gate_configs.sql.
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
        1: 50_000.0,
        2: 35_000.0,
        3: 20_000.0,
    },
    "exclude_indices": {
        1: 1.0,
        2: 1.0,
        3: 1.0,
    },
}

# Public alias — tests import _FALLBACK to assert default values directly.
# Keys are (gate_name, tier) tuples for backwards compatibility with
# parametrized tests that iterate _FALLBACK.keys().
_FALLBACK: dict[tuple[str, int], float] = {
    (gate, tier): value
    for gate, tiers in _DEFAULTS.items()
    for tier, value in tiers.items()
}


def _is_market_open() -> bool:
    """Return True if the US equity market is currently open (9:30–16:00 ET, Mon–Fri)."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now = datetime.now(ET)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    open_t  = time(9, 30)
    close_t = time(16, 0)
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

    Thread safety
    -------------
    * The internal ``_cache`` dict is replaced atomically on ``load()``.
    * Individual key writes on ``update()`` are protected by ``_lock``.
    * Plain reads of ``_cache[key]`` are GIL-safe and need no lock.
    """

    def __init__(self) -> None:
        # Nested dict: gate_name -> {tier -> value}
        # Initialised from _DEFAULTS; deep-copied so mutations are isolated.
        self._cache: dict[str, dict[int, float]] = {
            gate: dict(tiers) for gate, tiers in _DEFAULTS.items()
        }
        self._bounds_cache: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self.epoch: int = 0          # monotonic counter — incremented on every update

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_alias(gate_name: str) -> str:
        """Resolve a gate name alias to its canonical form. Identity for canonical names."""
        return _ALIAS_MAP.get(gate_name, gate_name)

    def get(self, gate_name: str, tier: int) -> float:
        """
        Return the current value for ``gate_name`` × ``tier``.

        Always returns float — 0.0 for unknown gates or missing tiers.
        Resolves 'debounce_ms' → 'signal_debounce_ms' transparently.
        Unknown tiers fall back to the T3 (_SAFE_DEFAULT_TIER) value.
        """
        gate_name = self._resolve_alias(gate_name)
        if gate_name not in _VALID_GATES:
            return 0.0
        tier_map = self._cache.get(gate_name, {})
        val = tier_map.get(tier)
        if val is None:
            # Unknown tier — fall back to _SAFE_DEFAULT_TIER (T3)
            val = tier_map.get(_SAFE_DEFAULT_TIER)
        if val is None:
            return 0.0
        return float(val)

    # ------------------------------------------------------------------
    # Startup load
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """
        Bulk-load all rows from gate_configs into the in-memory cache.

        Falls back silently to hard-coded ``_DEFAULTS`` if the DB is
        unreachable or the table does not yet exist (pre-migration).
        """
        from config import settings
        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_all_rows, settings.SUPABASE_URL,
                settings.SUPABASE_SERVICE_KEY,
            )
        except Exception as exc:
            log.warning(
                "[gate_config] DB load failed — using hardcoded defaults: %s", exc
            )
            return

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

        log.info(
            "[gate_config] Loaded %d rows from DB (epoch=%d)", len(rows), self.epoch
        )

    @staticmethod
    def _fetch_all_rows(url: str, key: str) -> list[dict]:
        from supabase import create_client
        sb = create_client(url, key)
        result = sb.table("gate_configs").select("*").execute()
        return result.data or []

    # ------------------------------------------------------------------
    # Write API (admin PATCH)
    # ------------------------------------------------------------------

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

        Returns
        -------
        dict with keys: gate_name, tier, old_value, new_value

        Raises
        ------
        ValueError  — unknown gate_name, bad tier, or value out of bounds
        RuntimeError — DB write failed
        """
        gate_name = self._resolve_alias(gate_name)
        if gate_name not in _VALID_GATES:
            raise ValueError(f"Unknown gate: {gate_name!r}")
        if tier not in _VALID_TIERS:
            raise ValueError(f"Invalid tier {tier!r} — must be one of {sorted(_VALID_TIERS)}")

        # Bounds check — use instance bounds_cache (from DB) if available,
        # otherwise fall back to the static _BOUNDS module constant.
        bounds = self._bounds_cache.get(gate_name)
        if bounds is None:
            raw = _BOUNDS.get(gate_name)
            if raw is not None:
                lo, hi = raw[0], raw[1]
            else:
                lo, hi = float("-inf"), float("inf")
        else:
            lo, hi = bounds
        if not (lo <= float(value) <= hi):
            raise ValueError(
                f"{gate_name!r} value {value} is outside allowed bounds [{lo}, {hi}]"
            )

        if _is_market_open() and not confirm_market_hours:
            raise ValueError(
                "Market is currently open — pass confirm_market_hours=True to force update"
            )

        old_value = self.get(gate_name, tier)

        # No-DB mode — update in-memory only.
        url = getattr(self, "_supabase_url", None)
        key = getattr(self, "_supabase_key", None)
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

        # DB-backed mode.
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
                raise RuntimeError(
                    f"DB PATCH failed: HTTP {patch_resp.status_code}"
                )

            # Audit row — best-effort; failure is non-fatal.
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
                        "changed_at": datetime.now(timezone.utc).isoformat(),
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

    # ------------------------------------------------------------------
    # Epoch parity guard
    # ------------------------------------------------------------------

    @staticmethod
    def assert_store_epoch_parity(
        gate_epoch: int,
        chain_epoch: int,
        universe_epoch: int,
    ) -> None:
        """
        Assert that dependent stores (chain, universe) have not advanced
        ahead of the gate store epoch.

        Pre-load zero values for chain_epoch or universe_epoch are always
        tolerated (no false positives at startup).

        Raises
        ------
        AssertionError — if chain_epoch > gate_epoch or universe_epoch > gate_epoch
                         (and the offending epoch is non-zero).
        """
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
