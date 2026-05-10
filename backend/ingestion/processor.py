"""
ingestion/processor.py — Ingestion floor enforcement.

Provides IngestionProcessor, the single gate that every parsed OptionsFlowEvent
must pass before it reaches the signal engines.  Gates are evaluated in order:

  1. DTE hard floor  (min_dte  — default 1, 0DTE never persists)
  2. DTE ceiling     (max_dte  — default 90)
  3. Tier-aware premium floor
     T1 (INSTITUTIONAL) : ing.min_premium.t1  (default $25 000)
     T2 (LARGE)         : ing.min_premium.t2  (default $15 000)
     T3 (RETAIL)        : ing.min_premium.t3  (default $5 000)
  4. Open-interest floor  (min_oi — default 0; new-chain events must reach
     signal engines — Vol>OI conviction gating is REARCH-006 signal logic)

Note on require_ask_tag (ing.require_ask_tag):
  This flag is stored in ingestion_config and exposed in IngestionConfig but
  does NOT gate events here.  is_aggressive is already tagged on OptionsFlowEvent
  by the parser (ING-006) and is consumed by signal engines in REARCH-006.

Note on symbol_registry constants:
  REGISTRY_MIN_OI / REGISTRY_MAX_DTE in symbol_registry.py control which
  contracts Tradier returns during chain-cache build — they are upstream of
  _process_trade() and intentionally separate enforcement points.  Do NOT
  conflate them with the gates here.

Public API:
  IngestionConfig                     frozen dataclass — immutable config snapshot
  IngestionProcessor                  main class
  IngestionProcessor.process(ev, tier=None)      Optional[OptionsFlowEvent]
  get_ingestion_config()              -> IngestionConfig  (cached, TTL=30 s)
  invalidate_ingestion_config_cache() -> None             (admin PATCH side-effect)

REARCH-002 (2026-05-09)

Fix (REARCH-010 2026-05-10): resolve influence_tier removal in _apply_gates.
  REARCH-010 dropped ev.influence_tier from OptionsFlowEvent (migration 024).
  _apply_gates previously read it via getattr(ev, "influence_tier", _TIER_T3),
  silently falling back to T3 floors for every event regardless of actual tier.
  Fix: process() and process_with_config() accept an optional `tier` int param
  (1=INSTITUTIONAL, 2=LARGE, 3/None=RETAIL).  _tier_int_to_str() converts the
  int to the string expected by _premium_floor().  When tier is supplied it
  takes precedence over any residual ev.influence_tier attribute.
  tradier_stream.py passes _ev_tier_int (resolved pre-parse from registry) so
  the correct per-ticker floor is enforced.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS: float = 30.0

# Tier string values as produced by influence_tier_string() in symbol_registry
_TIER_T1 = "INSTITUTIONAL"
_TIER_T2 = "LARGE"
_TIER_T3 = "RETAIL"

# ---------------------------------------------------------------------------
# Tier int -> string conversion (REARCH-010 fix)
# influence_tier_string() was deleted from SymbolRegistry; convert here so
# _apply_gates keeps working with the string-based _premium_floor() helper.
# ---------------------------------------------------------------------------

def _tier_int_to_str(tier_int: Optional[int]) -> str:
    """Convert an integer tier (1/2/3) to the legacy tier string for _premium_floor()."""
    if tier_int == 1:
        return _TIER_T1
    if tier_int == 2:
        return _TIER_T2
    return _TIER_T3


# ---------------------------------------------------------------------------
# IngestionConfig — immutable snapshot of DB-sourced floor values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestionConfig:
    """
    Immutable snapshot of ingestion floor parameters.

    Defaults are the hardcoded fallback used at cold-start and whenever the
    DB is unreachable.  The hot path reads a module-level reference to one of
    these objects — refresh creates a NEW instance and swaps the reference.
    Never mutate a live instance.
    """
    min_dte:         int  = 1
    max_dte:         int  = 90
    min_premium_t1:  int  = 25_000
    min_premium_t2:  int  = 15_000
    min_premium_t3:  int  = 5_000
    min_oi:          int  = 0      # 0 = pass new-chain events; Vol>OI gating is REARCH-006
    require_ask_tag: bool = True   # tagging only — does not gate events


# ---------------------------------------------------------------------------
# In-process cache — GIL-safe reference swap, TTL=30s
# ---------------------------------------------------------------------------

_cache: IngestionConfig = IngestionConfig()   # initialised from hardcoded defaults
_cache_expires_at: float = 0.0               # force refresh on first real call
_refresh_lock: Optional[asyncio.Lock] = None  # created lazily inside async context


def _get_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


async def _fetch_from_db() -> IngestionConfig:
    """
    Pull ingestion_config rows from Supabase and build a new IngestionConfig.
    Falls back to current cache if the query fails.
    """
    try:
        from services.supabase_client import get_supabase_client  # local import — avoids circular
        sb = get_supabase_client()
        resp = sb.table("ingestion_config").select("key,value,value_type").execute()
        rows = resp.data or []
        kv: dict = {}
        for row in rows:
            k, v, vt = row["key"], row["value"], row["value_type"]
            if vt == "int":
                kv[k] = int(v)
            elif vt == "float":
                kv[k] = float(v)
            elif vt == "bool":
                kv[k] = v.lower() in ("true", "1", "yes")
            else:
                kv[k] = v

        return IngestionConfig(
            min_dte=         kv.get("ing.min_dte",          IngestionConfig.min_dte),
            max_dte=         kv.get("ing.max_dte",          IngestionConfig.max_dte),
            min_premium_t1=  kv.get("ing.min_premium.t1",   IngestionConfig.min_premium_t1),
            min_premium_t2=  kv.get("ing.min_premium.t2",   IngestionConfig.min_premium_t2),
            min_premium_t3=  kv.get("ing.min_premium.t3",   IngestionConfig.min_premium_t3),
            min_oi=          kv.get("ing.min_oi",           IngestionConfig.min_oi),
            require_ask_tag= kv.get("ing.require_ask_tag",  IngestionConfig.require_ask_tag),
        )
    except Exception as exc:
        log.warning("ingestion_config DB fetch failed — keeping current cache: %s", exc)
        return _cache


def get_ingestion_config() -> IngestionConfig:
    """
    Return the current IngestionConfig snapshot.  Sub-microsecond on cache hit.
    Triggers a background async refresh if the TTL has elapsed.  Synchronous
    callers (e.g. the hot path in _process_trade) always get the last good
    snapshot immediately; the refresh runs on the next event-loop iteration.
    """
    global _cache, _cache_expires_at
    if time.monotonic() >= _cache_expires_at:
        # Schedule async refresh without blocking the caller.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_refresh_cache())
        except RuntimeError:
            # No running loop (e.g. unit tests) — defer until next async call.
            pass
    return _cache


async def _refresh_cache() -> None:
    """Fetch a fresh IngestionConfig from DB and swap the module-level reference."""
    global _cache, _cache_expires_at
    async with _get_lock():
        # Re-check expiry under lock — another task may have refreshed already.
        if time.monotonic() < _cache_expires_at:
            return
        new_cfg = await _fetch_from_db()
        _cache = new_cfg                                    # atomic reference swap (GIL-safe)
        _cache_expires_at = time.monotonic() + _CACHE_TTL_SECONDS
        log.debug("ingestion_config cache refreshed: %s", new_cfg)


def invalidate_ingestion_config_cache() -> None:
    """
    Force the next get_ingestion_config() call to trigger a cache refresh.
    Called by the admin PATCH handler immediately after a DB write.
    """
    global _cache_expires_at
    _cache_expires_at = 0.0
    log.info("ingestion_config cache invalidated by admin write")


# ---------------------------------------------------------------------------
# Drop stats — lightweight counters, no lock (GIL-safe int increments)
# ---------------------------------------------------------------------------

_stats: dict[str, int] = {
    "dropped_min_dte":     0,
    "dropped_max_dte":     0,
    "dropped_min_premium": 0,
    "dropped_min_oi":      0,
    "passed":              0,
}


def get_drop_stats() -> dict[str, int]:
    """Return a shallow copy of the drop stats dict."""
    return dict(_stats)


def reset_drop_stats() -> None:
    """Reset all counters to zero.  Intended for tests and diagnostics only."""
    for k in _stats:
        _stats[k] = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _premium_floor(cfg: IngestionConfig, tier: str) -> int:
    """Return the premium floor for the given tier string."""
    if tier == _TIER_T1:
        return cfg.min_premium_t1
    if tier == _TIER_T2:
        return cfg.min_premium_t2
    return cfg.min_premium_t3   # T3 / unknown — safest default


# ---------------------------------------------------------------------------
# IngestionProcessor
# ---------------------------------------------------------------------------

class IngestionProcessor:
    """
    Single-responsibility gate applied to every OptionsFlowEvent after parse
    and before signal-engine dispatch.

    Usage (in tradier_stream.py):

        processor = IngestionProcessor()
        ...
        # inside _process_trade(), after parse_tradier_trade():
        ev = processor.process(ev, tier=_ev_tier_int)
        if ev is None:
            return

    The processor is stateless beyond reading the shared config cache, so a
    single module-level instance in tradier_stream.py is sufficient.  It can
    also be instantiated directly in tests with an injected IngestionConfig
    via process_with_config().

    REARCH-010: process() and process_with_config() accept an optional `tier`
    int (1/2/3) to replace the now-removed ev.influence_tier attribute.  When
    supplied, `tier` takes precedence over any residual ev.influence_tier.
    """

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process(self, ev: object, tier: Optional[int] = None) -> Optional[object]:
        """
        Apply all ingestion gates to *ev* using the live cached IngestionConfig.
        Returns the event unchanged if it passes all gates, or None if dropped.

        tier: optional int (1=INSTITUTIONAL, 2=LARGE, 3/None=RETAIL).
          Pass _ev_tier_int from tradier_stream._process_trade() so the
          correct per-ticker premium floor is enforced after REARCH-010
          dropped ev.influence_tier from OptionsFlowEvent.

        Type is `object` here to avoid a circular import with the event models;
        callers hold the concrete OptionsFlowEvent type.  All attribute accesses
        are duck-typed against the expected event shape.
        """
        cfg = get_ingestion_config()
        return self._apply_gates(ev, cfg, tier=tier)

    def process_with_config(
        self,
        ev: object,
        cfg: IngestionConfig,
        tier: Optional[int] = None,
    ) -> Optional[object]:
        """
        Apply gates using an explicitly supplied IngestionConfig.  Intended for
        unit tests that need deterministic config without touching the cache.

        tier: optional int — same semantics as process().
        """
        return self._apply_gates(ev, cfg, tier=tier)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply_gates(
        self,
        ev: object,
        cfg: IngestionConfig,
        tier: Optional[int] = None,
    ) -> Optional[object]:
        dte           = getattr(ev, "dte",           0)
        premium       = getattr(ev, "premium",       0)
        open_interest = getattr(ev, "open_interest", 0)

        # REARCH-010: ev.influence_tier no longer exists (dropped in migration 024).
        # Resolve tier string from the int param supplied by tradier_stream
        # (_ev_tier_int from pre-parse registry lookup).  Fall back to the
        # legacy attribute for callers that still set it (e.g. older unit tests),
        # then default to T3 if neither is available.
        #
        # IMPORTANT: always pipe through _tier_int_to_str() — ev.influence_tier
        # is an int (1/2/3) and _premium_floor() compares against string
        # constants.  Without conversion, int 1 != "INSTITUTIONAL" and the T3
        # floor silently wins for every T1/T2 event.
        if tier is not None:
            influence_tier = _tier_int_to_str(tier)
        else:
            raw = getattr(ev, "influence_tier", None)
            influence_tier = _tier_int_to_str(raw) if isinstance(raw, int) else (raw or _TIER_T3)

        # Gate 1 — DTE hard floor
        if dte < cfg.min_dte:
            _stats["dropped_min_dte"] += 1
            return None

        # Gate 2 — DTE ceiling
        if dte > cfg.max_dte:
            _stats["dropped_max_dte"] += 1
            return None

        # Gate 3 — Tier-aware premium floor
        floor = _premium_floor(cfg, influence_tier)
        if premium < floor:
            _stats["dropped_min_premium"] += 1
            return None

        # Gate 4 — Open-interest floor (OI sourced from options_chain_cache)
        # Default is 0 — new-chain events with OI=0 pass through.
        # Vol>OI conviction gating belongs in signal engines (REARCH-006).
        if open_interest < cfg.min_oi:
            _stats["dropped_min_oi"] += 1
            return None

        _stats["passed"] += 1
        return ev
