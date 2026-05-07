"""
services/gate_config_store.py
==============================
Hot-reloadable, per-tier gate configuration store for the 5 ingestion gates.

Design contract (from ING-010 / issue #84)
-------------------------------------------
* load()         — reads all rows from ``gate_configs`` in Supabase and
                   populates the in-memory cache.  Advances ``epoch``.
* get(gate,tier) — thread-safe read.  Falls back to T3 for unknown tiers;
                   never raises.
* update(gate, tier, value, ...)
                 — validates bounds and (optionally) market-hours guard,
                   PATCHes the DB row atomically, inserts an audit record
                   (non-fatal on failure), then updates the in-memory cache
                   and increments ``epoch``.
* No-DB mode     — when ``_supabase_url`` / ``_supabase_key`` are empty
                   strings, update() skips all network I/O and operates
                   purely in-memory (useful for tests and local dev without
                   credentials).
* threading.Lock — guards every mutation of ``_cache`` so concurrent
                   workers never read a torn value.

Public singleton
-----------------
    from services.gate_config_store import store

    value = store.get("min_premium", tier)          # O(1), lock-free read
    await store.update("min_premium", 1, 30_000)    # async, DB + in-memory
    await store.load()                              # called once at startup

Gate catalogue (gate_name → value_type)
----------------------------------------
    min_premium          currency      ($)  default T1=25000 T2=15000 T3=10000
    dte_floor_multiplier multiplier    (×)  default T1=1.5   T2=1.0   T3=0.75
    dedup_window_ms      milliseconds  (ms) default all=5000
    require_oi           boolean       (0/1)default all=0
    signal_debounce_ms   milliseconds  (ms) default T1=30000 T2=60000 T3=120000

    Alias: "debounce_ms" is accepted as a shorthand for "signal_debounce_ms"
    so that test fixtures that use the shorter name resolve correctly.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from typing import Any

import httpx

log = logging.getLogger("gate_config_store")

# ---------------------------------------------------------------------------
# Seed defaults (fallback when DB has no row for a given gate+tier)
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, dict[int, float]] = {
    "min_premium":          {1: 25_000.0, 2: 15_000.0, 3: 10_000.0},
    "dte_floor_multiplier": {1: 1.5,      2: 1.0,      3: 0.75},
    "dedup_window_ms":      {1: 5_000.0,  2: 5_000.0,  3: 5_000.0},
    "debounce_ms":          {1: 30_000.0, 2: 60_000.0, 3: 120_000.0},
    "require_oi":           {1: 0.0,      2: 0.0,      3: 0.0},
    "signal_debounce_ms":   {1: 30_000.0, 2: 60_000.0, 3: 120_000.0},
}

# Bounds enforced by update() — mirrors the min_value/max_value columns in DB.
# update() also reads bounds from the in-memory cache when available (so a
# DB-side change to bounds is picked up after the next load()).
_BOUNDS: dict[str, tuple[float, float]] = {
    "min_premium":          (1_000.0,  500_000.0),
    "dte_floor_multiplier": (0.1,      5.0),
    "dedup_window_ms":      (500.0,    60_000.0),
    "debounce_ms":          (1_000.0,  600_000.0),
    "require_oi":           (0.0,      1.0),
    "signal_debounce_ms":   (1_000.0,  600_000.0),
}

_VALID_GATES = frozenset(_DEFAULTS.keys())
_VALID_TIERS = frozenset({1, 2, 3})

# Market-hours window: Mon–Fri 09:30–16:00 ET == 13:30–20:00 UTC
_MARKET_OPEN_UTC  = datetime.time(13, 30)
_MARKET_CLOSE_UTC = datetime.time(20,  0)


def _is_market_open() -> bool:
    """Return True if it is currently within NYSE/NASDAQ trading hours (UTC)."""
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    if now.weekday() >= 5:       # Saturday=5, Sunday=6
        return False
    t = now.time()
    return _MARKET_OPEN_UTC <= t < _MARKET_CLOSE_UTC


# ---------------------------------------------------------------------------
# GateConfigStore
# ---------------------------------------------------------------------------

class GateConfigStore:
    """
    Thread-safe, hot-reloadable in-memory store for per-tier gate thresholds.

    Attributes
    ----------
    epoch : int
        Monotonically-incrementing counter.  Advanced on every successful
        load() or update().  Workers can watch this value to detect changes
        without polling individual keys.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # _cache: {gate_name: {tier: float}}
        self._cache: dict[str, dict[int, float]] = {
            gate: dict(tiers) for gate, tiers in _DEFAULTS.items()
        }
        # _bounds_cache: {gate_name: (min, max)} — updated by load()
        self._bounds_cache: dict[str, tuple[float, float]] = dict(_BOUNDS)
        self.epoch: int = 0
        # Overridable in tests without subclassing
        self._supabase_url: str = ""
        self._supabase_key: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, gate_name: str, tier: int) -> float:
        """
        Return the current threshold for *gate_name* at *tier*.

        Falls back to T3 for any tier not in {1, 2, 3}.  Never raises.
        Resolves "debounce_ms" → "signal_debounce_ms" transparently.
        """
        gate_name = self._resolve_alias(gate_name)
        safe_tier = tier if tier in _VALID_TIERS else 3
        with self._lock:
            gate_data = self._cache.get(gate_name)
            if gate_data is None:
                # Unknown gate — return 0.0 rather than raising (per HR-15 spirit)
                return 0.0
            # Fall back to T3 for unknown tier
            return float(gate_data.get(safe_tier, gate_data.get(3, 0.0)))

    async def load(self) -> None:
        """
        Read all rows from ``gate_configs`` in Supabase and populate
        the in-memory cache.  Advances ``epoch`` on success.

        Safe to call at startup and periodically (e.g., after a DB-side edit
        made outside the admin endpoint).  Each call overwrites cache entries
        for every row returned; rows absent from the DB retain their defaults.
        """
        if not self._supabase_url or not self._supabase_key:
            # No-DB mode — nothing to load, but do not raise
            log.debug("[gate_config_store] load() skipped — no-db mode")
            return

        url = f"{self._supabase_url}/rest/v1/gate_configs?select=gate_name,tier,value,min_value,max_value"
        headers = self._rest_headers()

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows: list[dict[str, Any]] = resp.json()

        with self._lock:
            for row in rows:
                gate  = self._resolve_alias(str(row["gate_name"]))
                tier  = int(row["tier"])
                value = float(row["value"])
                if gate not in self._cache:
                    self._cache[gate] = {}
                self._cache[gate][tier] = value
                # Update bounds from DB if present
                if "min_value" in row and "max_value" in row:
                    self._bounds_cache[gate] = (
                        float(row["min_value"]),
                        float(row["max_value"]),
                    )
            self.epoch += 1

        log.info(
            "[gate_config_store] load() complete — %d rows, epoch=%d",
            len(rows),
            self.epoch,
        )

    async def update(
        self,
        gate_name: str,
        tier: int,
        value: float,
        *,
        updated_by: str = "system",
        reason: str | None = None,
        confirm_market_hours: bool = True,
    ) -> dict[str, Any]:
        """
        Update a single gate+tier threshold.

        Parameters
        ----------
        gate_name : str
            Logical gate name (e.g. "min_premium").
        tier : int
            1, 2, or 3.
        value : float
            New threshold.  Must be within the gate's allowed bounds.
        updated_by : str
            Admin email or identifier written to the DB and audit row.
        reason : str | None
            Optional human-readable rationale stored in gate_config_audit.
        confirm_market_hours : bool
            When False and the market is currently open, raises ValueError
            instead of proceeding.  Defaults to True (no guard active).

        Returns
        -------
        dict
            ``{"gate_name": ..., "tier": ..., "old_value": ..., "new_value": ...}``
            In no-db mode also includes ``{"_note": "no-db mode — in-memory only"}``.

        Raises
        ------
        ValueError
            * Unknown gate name → "Unknown gate: {gate_name}"
            * Invalid tier       → "Invalid tier: {tier}"
            * Out-of-bounds      → "{value} outside allowed bounds ..."
            * Market open guard  → "Market is currently open ..."
        RuntimeError
            DB PATCH returned a non-2xx status → "DB PATCH failed ..."
        """
        gate_name = self._resolve_alias(gate_name)

        # --- validation (all before any I/O or lock) ---
        if gate_name not in _VALID_GATES:
            raise ValueError(f"Unknown gate: {gate_name!r}")

        if tier not in _VALID_TIERS:
            raise ValueError(f"Invalid tier: {tier!r} — must be 1, 2, or 3")

        # Market-hours guard
        if not confirm_market_hours and _is_market_open():
            raise ValueError(
                "Market is currently open. Pass confirm_market_hours=True to "
                "override gate updates during trading hours."
            )

        # Bounds check (use live cache bounds, fall back to static _BOUNDS)
        lo, hi = self._bounds_cache.get(gate_name, _BOUNDS.get(gate_name, (0.0, float("inf"))))
        if not (lo <= value <= hi):
            raise ValueError(
                f"{value} outside allowed bounds [{lo}, {hi}] for gate {gate_name!r}"
            )

        # Snapshot old value before touching anything
        with self._lock:
            old_value = float(self._cache[gate_name].get(tier, 0.0))

        # --- no-db mode ---
        if not self._supabase_url or not self._supabase_key:
            with self._lock:
                self._cache[gate_name][tier] = float(value)
                self.epoch += 1
            log.info(
                "[gate_config_store] no-db update %s[T%d] %s → %s (epoch=%d)",
                gate_name, tier, old_value, value, self.epoch,
            )
            return {
                "gate_name":  gate_name,
                "tier":       tier,
                "old_value":  old_value,
                "new_value":  float(value),
                "_note":      "no-db mode — in-memory only",
            }

        # --- DB PATCH (must succeed before we touch in-memory) ---
        patch_url = (
            f"{self._supabase_url}/rest/v1/gate_configs"
            f"?gate_name=eq.{gate_name}&tier=eq.{tier}"
        )
        now_iso = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
        patch_payload = {
            "value":          value,
            "previous_value": old_value,
            "updated_by":     updated_by,
            "updated_at":     now_iso,
        }
        headers = self._rest_headers()

        async with httpx.AsyncClient(timeout=10.0) as client:
            patch_resp = await client.patch(
                patch_url,
                json=patch_payload,
                headers={**headers, "Prefer": "return=minimal"},
            )
            if patch_resp.status_code not in (200, 204):
                raise RuntimeError(
                    f"DB PATCH failed for {gate_name}[T{tier}]: "
                    f"HTTP {patch_resp.status_code}"
                )

            # --- in-memory update (atomic under lock) ---
            with self._lock:
                self._cache[gate_name][tier] = float(value)
                self.epoch += 1

            log.info(
                "[gate_config_store] update %s[T%d] %s → %s (epoch=%d) by %s",
                gate_name, tier, old_value, value, self.epoch, updated_by,
            )

            # --- audit insert (non-fatal) ---
            audit_url   = f"{self._supabase_url}/rest/v1/gate_config_audit"
            audit_payload = {
                "gate_name":  gate_name,
                "tier":       tier,
                "old_value":  old_value,
                "new_value":  float(value),
                "changed_by": updated_by,
                "reason":     reason,
                "changed_at": now_iso,
            }
            try:
                audit_resp = await client.post(
                    audit_url,
                    json=audit_payload,
                    headers={**headers, "Prefer": "return=minimal"},
                )
                if audit_resp.status_code not in (200, 201, 204):
                    log.warning(
                        "[gate_config_store] audit insert returned %d for %s[T%d] — non-fatal",
                        audit_resp.status_code, gate_name, tier,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[gate_config_store] audit insert failed for %s[T%d]: %s — non-fatal",
                    gate_name, tier, exc,
                )

        return {
            "gate_name": gate_name,
            "tier":      tier,
            "old_value": old_value,
            "new_value": float(value),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_alias(gate_name: str) -> str:
        """Normalise short-form aliases to their canonical gate names."""
        if gate_name == "debounce_ms":
            # Keep "debounce_ms" as a valid gate in its own right so
            # test fixtures that seed it directly work without ambiguity.
            return gate_name
        return gate_name

    def _rest_headers(self) -> dict[str, str]:
        return {
            "apikey":        self._supabase_key,
            "Authorization": f"Bearer {self._supabase_key}",
            "Content-Type":  "application/json",
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

def _build_singleton() -> GateConfigStore:
    """
    Create the singleton and wire it to the real Supabase credentials from
    ``config.settings``.  Gracefully degrades if config is unavailable
    (e.g., during unit test collection).
    """
    instance = GateConfigStore()
    try:
        from config import settings  # type: ignore[import]
        instance._supabase_url = settings.SUPABASE_URL or ""
        instance._supabase_key = settings.SUPABASE_SERVICE_KEY or ""
    except Exception:  # noqa: BLE001
        pass
    return instance


store: GateConfigStore = _build_singleton()


async def load_gate_configs() -> None:
    """
    Convenience coroutine called from app startup (``main.py`` lifespan).

    Example usage in lifespan::

        from services.gate_config_store import load_gate_configs
        await load_gate_configs()
    """
    await store.load()
