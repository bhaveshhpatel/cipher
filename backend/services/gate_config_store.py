from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, time as dt_time
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gate name constants — single source of truth for callers
# ---------------------------------------------------------------------------
GATE_MIN_PREMIUM = "min_premium"
GATE_DTE_FLOOR_MULTIPLIER = "dte_floor_multiplier"
GATE_REQUIRE_OI = "require_oi"
GATE_SIGNAL_DEBOUNCE_MS = "signal_debounce_ms"
GATE_DEDUP_WINDOW_MS = "dedup_window_ms"  # seeded; Python wiring deferred

# ---------------------------------------------------------------------------
# Hardcoded fallback defaults — cold-start guard only.
# Primary source of truth is the gate_configs DB table (seeded in 013 migration).
# ---------------------------------------------------------------------------
_FALLBACK_DEFAULTS: dict[str, float] = {
    GATE_MIN_PREMIUM: 10_000.0,
    GATE_DTE_FLOOR_MULTIPLIER: 1.0,
    GATE_REQUIRE_OI: 0.0,
    GATE_SIGNAL_DEBOUNCE_MS: 60_000.0,
    GATE_DEDUP_WINDOW_MS: 5_000.0,
}

# ---------------------------------------------------------------------------
# Validation bounds — hardcoded per deliberation (D3 / PBE).
# Move to a DB table in a follow-on story if runtime-configurable bounds are needed.
# ---------------------------------------------------------------------------
_GATE_BOUNDS: dict[str, tuple[float, float]] = {
    GATE_MIN_PREMIUM:          (1_000.0,   500_000.0),
    GATE_DTE_FLOOR_MULTIPLIER: (0.1,       5.0),
    GATE_REQUIRE_OI:           (0.0,       1.0),
    GATE_SIGNAL_DEBOUNCE_MS:   (1_000.0,   3_600_000.0),
    GATE_DEDUP_WINDOW_MS:      (500.0,     60_000.0),
}

# Market hours guard — ET (UTC-4 summer / UTC-5 winter). Use UTC comparison.
_MARKET_OPEN_ET  = dt_time(13, 30)   # 09:30 ET = 13:30 UTC
_MARKET_CLOSE_ET = dt_time(20, 0)    # 16:00 ET = 20:00 UTC

# ---------------------------------------------------------------------------
# Module-level stats — cold-start safe (initialised before first tick)
# ---------------------------------------------------------------------------
_stats: dict[str, int] = {
    "db_loads": 0,
    "updates": 0,
    "db_write_failures": 0,
    "retry_queue_flushes": 0,
}


class GateConfigValidationError(ValueError):
    """Raised when a submitted gate config value violates bounds."""


class MarketHoursConfirmationRequired(Exception):
    """Raised when a market-hours change is submitted without confirm_market_hours=True."""


class GateConfigStore:
    """
    Thread-safe in-memory config store for ingestion gate thresholds.

    Loaded at startup from DB. Updated via admin API without process restart.
    Workers hold a reference to the process-level singleton and read live values
    on every gate check via get_threshold().

    Single-process Railway deployment: in-memory singleton is sufficient.
    Multi-replica follow-on: add Supabase Realtime subscription that calls
    load_from_db() on any gate_configs UPDATE event.
    """

    _instance: Optional["GateConfigStore"] = None

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Keyed by (gate_name, tier) -> float
        self._configs: dict[tuple[str, int], float] = {}
        self._last_loaded: Optional[datetime] = None
        # Failed DB writes are queued for async retry
        self._retry_queue: list[dict] = []

    # ------------------------------------------------------------------
    # Singleton accessor
    # ------------------------------------------------------------------
    @classmethod
    def get(cls) -> "GateConfigStore":
        if cls._instance is None:
            cls._instance = GateConfigStore()
        return cls._instance

    @classmethod
    def reset_for_testing(cls) -> None:
        """Reset singleton state. Only call from tests."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Startup load
    # ------------------------------------------------------------------
    async def load_from_db(self, db) -> None:
        """
        Load all gate_configs rows from Supabase into memory.
        Called at startup and by admin PATCH after a successful write.
        On failure: logs error, leaves existing in-memory state intact.
        """
        try:
            result = await db.table("gate_configs").select(
                "gate_name, tier, value"
            ).execute()
            new_configs: dict[tuple[str, int], float] = {
                (row["gate_name"], int(row["tier"])): float(row["value"])
                for row in (result.data or [])
            }
            async with self._lock:
                self._configs = new_configs
                self._last_loaded = datetime.now(timezone.utc)
            _stats["db_loads"] += 1
            log.info(
                "gate_config_store loaded %d rows from db",
                len(new_configs),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("gate_config_store: load_from_db failed: %s", exc)

    # ------------------------------------------------------------------
    # Hot read — called on every tick, must not block
    # ------------------------------------------------------------------
    def get_threshold(self, gate_name: str, symbol: str) -> float:
        """
        Return the gate threshold for (gate_name, symbol).
        Tier is resolved from tier_engine with T3 as safe default.
        Never performs I/O — pure in-memory dict lookup.
        """
        tier = self._resolve_tier(symbol)
        value = self._configs.get((gate_name, tier))
        if value is None:
            value = _FALLBACK_DEFAULTS.get(gate_name, 0.0)
            log.debug(
                "gate_config_store: fallback for gate=%s symbol=%s tier=%d value=%s",
                gate_name,
                symbol,
                tier,
                value,
            )
        return value

    def get_threshold_for_tier(self, gate_name: str, tier: int) -> float:
        """Direct tier lookup — used by admin GET and tests."""
        value = self._configs.get((gate_name, tier))
        if value is None:
            return _FALLBACK_DEFAULTS.get(gate_name, 0.0)
        return value

    # ------------------------------------------------------------------
    # Admin write — called by PATCH /api/admin/gate-config
    # ------------------------------------------------------------------
    async def update(
        self,
        gate_name: str,
        tier: int,
        value: float,
        updated_by: str,
        reason: str,
        db,
        confirm_market_hours: bool = False,
    ) -> dict:
        """
        Validate, update in-memory, persist to DB.
        Returns a dict with old_value, new_value, propagated_at.
        Raises GateConfigValidationError on bound violations.
        Raises MarketHoursConfirmationRequired when confirm_market_hours is missing.
        """
        self._validate(gate_name, value)
        self._check_market_hours(confirm_market_hours)

        old_value = self._configs.get((gate_name, tier))

        async with self._lock:
            self._configs[(gate_name, tier)] = value

        _stats["updates"] += 1
        propagated_at = datetime.now(timezone.utc)

        payload = {
            "gate_name": gate_name,
            "tier": tier,
            "value": value,
            "updated_by": updated_by,
            "previous_value": old_value,
            "reason": reason,
            "updated_at": propagated_at.isoformat(),
        }

        await self._persist_or_queue(payload, db)

        log.info(
            "gate_config updated gate=%s tier=%d old=%s new=%s by=%s",
            gate_name,
            tier,
            old_value,
            value,
            updated_by,
        )

        return {
            "gate_name": gate_name,
            "tier": tier,
            "old_value": old_value,
            "new_value": value,
            "propagated_at": propagated_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # Admin read helpers
    # ------------------------------------------------------------------
    def get_all(self) -> dict[tuple[str, int], float]:
        """Return a snapshot of the full config map. Used by GET endpoint."""
        return dict(self._configs)

    def get_bounds(self) -> dict[str, dict]:
        """Return validation bounds per gate. Included in GET response for admin UI."""
        return {
            name: {"min": lo, "max": hi}
            for name, (lo, hi) in _GATE_BOUNDS.items()
        }

    def get_stats(self) -> dict:
        return dict(_stats)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve_tier(self, symbol: str) -> int:
        """
        Resolve symbol to tier via tier_engine.
        Returns 3 (T3) as safe default for unknown symbols.
        Import is deferred inside the method to avoid circular imports at
        module load time — tier_engine may import from services.
        """
        try:
            from backend.services.tier_engine import get_symbol_tier  # noqa: PLC0415
            tier = get_symbol_tier(symbol)
            if tier not in (1, 2, 3):
                return 3
            return tier
        except Exception:  # noqa: BLE001
            return 3

    def _validate(self, gate_name: str, value: float) -> None:
        bounds = _GATE_BOUNDS.get(gate_name)
        if bounds is None:
            raise GateConfigValidationError(f"Unknown gate: {gate_name!r}")
        lo, hi = bounds
        if not (lo <= value <= hi):
            raise GateConfigValidationError(
                f"Gate {gate_name!r} value {value} out of bounds [{lo}, {hi}]"
            )

    def _check_market_hours(self, confirmed: bool) -> None:
        now_utc = datetime.now(timezone.utc).time()
        if _MARKET_OPEN_ET <= now_utc <= _MARKET_CLOSE_ET and not confirmed:
            raise MarketHoursConfirmationRequired(
                "Config change during market hours requires confirm_market_hours=true"
            )

    async def _persist_or_queue(self, payload: dict, db) -> None:
        try:
            await db.table("gate_configs").upsert(
                payload, on_conflict="gate_name,tier"
            ).execute()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "gate_config_store: DB write failed for %s tier=%d, queuing for retry: %s",
                payload["gate_name"],
                payload["tier"],
                exc,
            )
            _stats["db_write_failures"] += 1
            self._retry_queue.append(payload)

    async def flush_retry_queue(self, db) -> int:
        """
        Attempt to persist any queued failed writes.
        Returns the count of successfully flushed items.
        Call this from a background task or after the next successful DB connection.
        """
        if not self._retry_queue:
            return 0
        flushed = 0
        remaining = []
        for payload in self._retry_queue:
            try:
                await db.table("gate_configs").upsert(
                    payload, on_conflict="gate_name,tier"
                ).execute()
                flushed += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("gate_config_store: retry flush failed for %s: %s", payload, exc)
                remaining.append(payload)
        self._retry_queue = remaining
        if flushed:
            _stats["retry_queue_flushes"] += flushed
            log.info("gate_config_store: flushed %d queued writes", flushed)
        return flushed
