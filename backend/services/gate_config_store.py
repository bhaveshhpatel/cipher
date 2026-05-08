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

# ---------------------------------------------------------------------------
# Hard-coded defaults — used when the DB is unreachable or a row is missing.
# Keys are (gate_name, tier) tuples.
# These MUST match the seed rows in 019_gate_configs.sql.
# ---------------------------------------------------------------------------
_DEFAULTS: dict[tuple[str, int], float] = {
    # min_premium
    ("min_premium",          1): 25_000.0,
    ("min_premium",          2): 15_000.0,
    ("min_premium",          3): 10_000.0,
    # dte_floor_multiplier
    ("dte_floor_multiplier", 1): 1.5,
    ("dte_floor_multiplier", 2): 1.0,
    ("dte_floor_multiplier", 3): 0.75,
    # dedup_window_ms
    ("dedup_window_ms",      1): 5_000.0,
    ("dedup_window_ms",      2): 5_000.0,
    ("dedup_window_ms",      3): 5_000.0,
    # require_oi
    ("require_oi",           1): 0.0,
    ("require_oi",           2): 0.0,
    ("require_oi",           3): 0.0,
    # signal_debounce_ms
    ("signal_debounce_ms",   1):  30_000.0,
    ("signal_debounce_ms",   2):  60_000.0,
    ("signal_debounce_ms",   3): 120_000.0,
    # signal_min_premium
    ("signal_min_premium",   1): 50_000.0,
    ("signal_min_premium",   2): 35_000.0,
    ("signal_min_premium",   3): 20_000.0,
    # exclude_indices
    ("exclude_indices",      1): 1.0,
    ("exclude_indices",      2): 1.0,
    ("exclude_indices",      3): 1.0,
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
        self._cache: dict[tuple[str, int], float] = dict(_DEFAULTS)
        self._bounds_cache: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self.epoch: int = 0          # monotonic counter — incremented on every update

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get(self, gate_name: str, tier: int) -> float:
        """
        Return the current value for ``gate_name`` × ``tier``.

        Always returns float — 0.0 for unknown gates or missing tiers.
        Resolves 'debounce_ms' → 'signal_debounce_ms' transparently.
        """
        gate_name = _ALIAS_MAP.get(gate_name, gate_name)
        if gate_name not in _VALID_GATES:
            return 0.0
        key = (gate_name, tier)
        val = self._cache.get(key)
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

        new_cache: dict[tuple[str, int], float] = dict(_DEFAULTS)
        new_bounds: dict[str, tuple[float, float]] = {}
        for row in rows:
            name = row.get("gate_name", "")
            t    = row.get("tier")
            val  = row.get("value")
            lo   = row.get("min_value", 0.0)
            hi   = row.get("max_value", float("inf"))
            if name and t is not None and val is not None:
                new_cache[(name, int(t))] = float(val)
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
        updated_by: str,
        reason: str | None = None,
        confirm_market_hours: bool = False,
    ) -> dict[str, Any]:
        """
        Commit a gate value change to the DB and hot-patch the cache.

        Returns
        -------
        dict with keys: old_value, new_value

        Raises
        ------
        ValueError  — unknown gate_name, bad tier, or value out of bounds
        RuntimeError — DB write failed
        """
        from config import settings

        gate_name = _ALIAS_MAP.get(gate_name, gate_name)
        if gate_name not in _VALID_GATES:
            raise ValueError(f"Unknown gate: {gate_name!r}")
        if tier not in (1, 2, 3):
            raise ValueError(f"tier must be 1, 2, or 3 — got {tier!r}")

        old_value = self.get(gate_name, tier)

        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._write_to_db,
                settings.SUPABASE_URL,
                settings.SUPABASE_SERVICE_KEY,
                gate_name, tier, value, old_value, updated_by, reason,
            )
        except Exception as exc:
            raise RuntimeError(f"DB write failed: {exc}") from exc

        with self._lock:
            self._cache[(gate_name, tier)] = value
            self.epoch += 1

        log.info(
            "[gate_config] %s[T%d] updated: %s -> %s by %s (epoch=%d)",
            gate_name, tier, old_value, value, updated_by, self.epoch,
        )
        return {"old_value": old_value, "new_value": value}

    @staticmethod
    def _write_to_db(
        url: str,
        key: str,
        gate_name: str,
        tier: int,
        value: float,
        old_value: float,
        updated_by: str,
        reason: str | None,
    ) -> None:
        from supabase import create_client
        sb = create_client(url, key)

        # 1. Update the live value
        sb.table("gate_configs").update({
            "value":      value,
            "updated_by": updated_by,
        }).eq("gate_name", gate_name).eq("tier", tier).execute()

        # 2. Append audit row — uses old_value/new_value per DDL in 019_gate_configs.sql
        sb.table("gate_config_audit").insert({
            "gate_name":  gate_name,
            "tier":       tier,
            "old_value":  old_value,
            "new_value":  value,
            "changed_by": updated_by,
            "reason":     reason,
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()


# Module-level singleton — imported by all consumers.
store = GateConfigStore()
gate_config_store = store   # alias used by main.py lifespan
