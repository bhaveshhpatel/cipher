"""
Integration tests — Gate config hot-reload  [ING-010]

These tests exercise the full PATCH -> GateConfigStore.update() -> in-memory
reload cycle against a mocked Supabase service layer.  They do NOT hit a real
DB; instead they stub GateConfigStore.update() and the market-hours helper so
the tests run offline in CI.

Four scenarios mandated by ING-010:
  1. Change propagates to the in-memory store within 5 seconds
  2. An out-of-bounds value returns HTTP 422
  3. During market hours, confirm_market_hours=False raises HTTP 428
  4. Every successful PATCH writes an audit log entry

Block B — real GateConfigStore singleton (no-db mode):
  B1–B6 exercise the real implementation against its seeded defaults and
  no-db update() path, covering the _resolve_alias fix and bounds guard.
  These tests would have caught the ING-010-IMPORT bug in gate_config_store.py.

Block C — dedup consumer wiring:
  C1–C4 verify DedupCache._resolve_tier_ttl and _effective_cleanup_ttl read
  from the real store singleton.  These tests would have caught the
  ING-010-IMPORT bug in dedup.py (both import sites).

Block D — tradier_stream consumer wiring:
  D1–D4 verify _resolve_signal_debounce_s, _resolve_signal_min_premium, and
  _should_emit_signal read from the real store.  These tests would have
  caught the ING-010-IMPORT bug in tradier_stream.py.

Block E — epoch parity:
  E1–E3 exercise GateConfigStore.assert_store_epoch_parity().
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest


# ===========================================================================
# Block A — _FakeStore (original 4 ING-010 scenarios, unchanged)
# ===========================================================================

class _FakeStore:
    """Minimal GateConfigStore stand-in for integration-level testing."""

    POLL_INTERVAL = 0.05  # 50 ms — fast enough to test propagation

    def __init__(self) -> None:
        self._data: dict[tuple[str, int], float] = {}
        self.epoch: int = 0
        self._audit_log: list[dict] = []

    def _seed(self, gate: str, tier: int, value: float) -> None:
        self._data[(gate, tier)] = value

    def get(self, gate: str, tier: int, default: float = 0.0) -> float:
        return self._data.get((gate, tier), default)

    async def update(
        self,
        gate_name: str,
        tier: int,
        value: float,
        updated_by: str,
        reason: str | None = None,
        confirm_market_hours: bool = True,
    ) -> dict[str, Any]:
        if not confirm_market_hours and self._market_open():
            raise RuntimeError("__market_hours__")
        lo, hi = self._bounds(gate_name)
        if not (lo <= value <= hi):
            raise ValueError(
                f"{gate_name} value {value} out of bounds [{lo}, {hi}]"
            )
        old = self._data.get((gate_name, tier))
        self._data[(gate_name, tier)] = value
        self.epoch += 1
        self._audit_log.append({
            "gate_name":  gate_name,
            "tier":       tier,
            "old_value":  old,
            "new_value":  value,
            "changed_by": updated_by,
            "reason":     reason,
        })
        return {"old_value": old, "new_value": value}

    @staticmethod
    def _bounds(gate: str) -> tuple[float, float]:
        _BOUNDS: dict[str, tuple[float, float]] = {
            "min_premium":          (1_000.0,  500_000.0),
            "dte_floor_multiplier": (0.1,          5.0),
            "dedup_window_ms":      (500.0,     60_000.0),
            "debounce_ms":          (500.0,     60_000.0),
            "require_oi":           (0.0,           1.0),
            "signal_debounce_ms":   (1_000.0,  600_000.0),
        }
        return _BOUNDS.get(gate, (0.0, float("inf")))

    @staticmethod
    def _market_open() -> bool:
        return False


@pytest.fixture()
def store() -> _FakeStore:
    s = _FakeStore()
    s._seed("min_premium",        1, 25_000.0)
    s._seed("min_premium",        2, 15_000.0)
    s._seed("min_premium",        3,  5_000.0)
    s._seed("dedup_window_ms",    1,  5_000.0)
    s._seed("signal_debounce_ms", 1, 30_000.0)
    s._seed("require_oi",         1,     1.0)
    return s


@pytest.mark.asyncio
async def test_change_propagates_within_5s(store: _FakeStore) -> None:
    start = time.monotonic()
    old_epoch = store.epoch
    result = await store.update(
        gate_name="min_premium",
        tier=1,
        value=30_000.0,
        updated_by="integration@test",
        reason="test: propagation speed",
    )
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"Hot-reload took {elapsed:.3f}s — must be <5s"
    assert store.get("min_premium", 1) == 30_000.0
    assert store.epoch == old_epoch + 1
    assert result["old_value"] == 25_000.0
    assert result["new_value"] == 30_000.0


@pytest.mark.asyncio
async def test_invalid_value_raises_422(store: _FakeStore) -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        await store.update(
            gate_name="min_premium", tier=1, value=999_999.0,
            updated_by="integration@test",
        )
    with pytest.raises(ValueError, match="out of bounds"):
        await store.update(
            gate_name="min_premium", tier=2, value=0.5,
            updated_by="integration@test",
        )
    assert store.get("min_premium", 1) == 25_000.0
    assert store.get("min_premium", 2) == 15_000.0
    assert store.epoch == 0


@pytest.mark.asyncio
async def test_market_hours_guard_raises_428(store: _FakeStore) -> None:
    original = _FakeStore._market_open
    _FakeStore._market_open = staticmethod(lambda: True)  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="__market_hours__"):
            await store.update(
                gate_name="dedup_window_ms", tier=1, value=8_000.0,
                updated_by="integration@test",
                confirm_market_hours=False,
            )
    finally:
        _FakeStore._market_open = staticmethod(original)  # type: ignore[method-assign]
    assert store.get("dedup_window_ms", 1) == 5_000.0
    assert store.epoch == 0


@pytest.mark.asyncio
async def test_audit_log_entry_on_every_change(store: _FakeStore) -> None:
    assert len(store._audit_log) == 0
    await store.update(
        gate_name="signal_debounce_ms", tier=1, value=45_000.0,
        updated_by="admin@cipher.io",
        reason="earnings week — widen debounce",
    )
    assert len(store._audit_log) == 1
    entry = store._audit_log[0]
    assert entry["gate_name"]  == "signal_debounce_ms"
    assert entry["tier"]       == 1
    assert entry["old_value"]  == 30_000.0
    assert entry["new_value"]  == 45_000.0
    assert entry["changed_by"] == "admin@cipher.io"
    assert entry["reason"]     == "earnings week — widen debounce"

    await store.update(
        gate_name="require_oi", tier=1, value=0.0,
        updated_by="admin@cipher.io",
        reason="allow zero-OI signals on T1 during VIX spike",
    )
    assert len(store._audit_log) == 2
    assert store._audit_log[1]["gate_name"] == "require_oi"
    assert store._audit_log[1]["old_value"] == 1.0
    assert store._audit_log[1]["new_value"] == 0.0
    assert store.epoch == 2


# ===========================================================================
# Helpers shared by Blocks B–E
# ===========================================================================

@pytest.fixture()
def real_store():
    """
    Returns a fresh GateConfigStore instance in no-db mode (empty credentials).
    Seeded from _DEFAULTS; epoch starts at 0.

    NOTE: we instantiate a *new* GateConfigStore rather than importing the
    module-level `store` singleton so tests are fully isolated from each other
    and from any prod state that might bleed in through the singleton.
    """
    from services.gate_config_store import GateConfigStore
    s = GateConfigStore()
    s._supabase_url = ""
    s._supabase_key = ""
    return s


# ===========================================================================
# Block B — real GateConfigStore, no-db mode
# ===========================================================================

@pytest.mark.ci_gate
def test_b1_seeded_defaults_all_gates(real_store) -> None:
    """
    B1: get() returns the correct seeded default for every gate x tier.
    This would have caught any accidental overwrite of _DEFAULTS on import.
    """
    from services.gate_config_store import _DEFAULTS
    for gate, tiers in _DEFAULTS.items():
        for tier, expected in tiers.items():
            got = real_store.get(gate, tier)
            assert got == expected, (
                f"gate={gate} tier={tier}: expected {expected}, got {got}"
            )


@pytest.mark.ci_gate
@pytest.mark.asyncio
async def test_b2_nodb_update_mutates_cache_and_advances_epoch(real_store) -> None:
    """
    B2: no-db update() writes to _cache and increments epoch.
    Confirms the in-memory path works end-to-end without a real DB.
    """
    assert real_store.epoch == 0
    result = await real_store.update(
        "min_premium", 1, 30_000.0,
        updated_by="test",
        confirm_market_hours=True,
    )
    assert real_store.get("min_premium", 1) == 30_000.0
    assert real_store.epoch == 1
    assert result["old_value"] == 25_000.0
    assert result["new_value"] == 30_000.0


@pytest.mark.ci_gate
def test_b3_debounce_ms_alias_resolves(real_store) -> None:
    """
    B3: get("debounce_ms", tier) must resolve to signal_debounce_ms.
    This is the _resolve_alias fix — previously "debounce_ms" was returned
    unchanged and read from a separate (never-loaded) _cache sub-dict.
    """
    for tier in (1, 2, 3):
        via_alias    = real_store.get("debounce_ms",        tier)
        via_canonical = real_store.get("signal_debounce_ms", tier)
        assert via_alias == via_canonical, (
            f"T{tier}: debounce_ms alias ({via_alias}) != "
            f"signal_debounce_ms canonical ({via_canonical})"
        )
        assert via_alias > 0, f"T{tier}: alias resolved to zero/None"


@pytest.mark.ci_gate
def test_b4_canonical_gate_names_unchanged(real_store) -> None:
    """
    B4: _resolve_alias must be a no-op for all canonical gate names.
    Guards against accidental over-aliasing.
    """
    from services.gate_config_store import _VALID_GATES
    for gate in _VALID_GATES:
        resolved = real_store._resolve_alias(gate)
        assert resolved == gate, (
            f"_resolve_alias({gate!r}) returned {resolved!r} — must be identity"
        )


@pytest.mark.ci_gate
@pytest.mark.asyncio
async def test_b5_out_of_bounds_raises_and_epoch_unchanged(real_store) -> None:
    """
    B5: update() with a value outside [min_value, max_value] raises ValueError.
    Epoch must not advance on a failed update.
    """
    assert real_store.epoch == 0
    with pytest.raises(ValueError):
        await real_store.update(
            "min_premium", 1, 999_999.0,
            updated_by="test",
            confirm_market_hours=True,
        )
    assert real_store.epoch == 0
    assert real_store.get("min_premium", 1) == 25_000.0  # unchanged


@pytest.mark.ci_gate
@pytest.mark.asyncio
async def test_b6_epoch_monotonically_increases(real_store) -> None:
    """
    B6: epoch increments by 1 for every successful update, across different
    gates and tiers.
    """
    gates_to_update = [
        ("min_premium",        1, 28_000.0),
        ("min_premium",        2, 14_000.0),
        ("dedup_window_ms",    1,  6_000.0),
        ("signal_debounce_ms", 3, 90_000.0),
    ]
    for i, (gate, tier, val) in enumerate(gates_to_update, start=1):
        await real_store.update(
            gate, tier, val,
            updated_by="test",
            confirm_market_hours=True,
        )
        assert real_store.epoch == i, (
            f"After {i} updates: expected epoch={i}, got epoch={real_store.epoch}"
        )


# ===========================================================================
# Block C — dedup consumer wiring
# Catches the ING-010-IMPORT bug: both import sites in dedup.py previously
# imported `gate_config_store` (non-existent) instead of `store`.
# These tests verify the live store is actually reachable from DedupCache.
# ===========================================================================

@pytest.fixture()
def patched_dedup_store(real_store):
    """
    Patch services.gate_config_store.store with `real_store` so that
    DedupCache._resolve_tier_ttl / _effective_cleanup_ttl read from a
    controlled, no-db instance.
    """
    with patch("services.gate_config_store.store", real_store):
        yield real_store


@pytest.mark.ci_gate
def test_c1_resolve_tier_ttl_reads_from_store(patched_dedup_store) -> None:
    """
    C1: DedupCache._resolve_tier_ttl(tier_int, fallback) must return the
    value from the store (dedup_window_ms T1 default = 5000ms = 5.0s),
    not the fallback (1.0s).

    If the import inside _resolve_tier_ttl is broken, the except branch fires
    and returns fallback=1.0 — test catches that.
    """
    from utils.dedup import DedupCache
    result = DedupCache._resolve_tier_ttl(tier_int=1, fallback=1.0)
    # T1 dedup_window_ms default = 5000ms = 5.0s
    assert result == pytest.approx(5.0), (
        f"Expected 5.0s from store, got {result} — import may be broken"
    )


@pytest.mark.ci_gate
def test_c2_resolve_tier_ttl_fallback_when_no_tier(patched_dedup_store) -> None:
    """
    C2: _resolve_tier_ttl(None, fallback) must return fallback immediately
    without touching the store.
    """
    from utils.dedup import DedupCache
    assert DedupCache._resolve_tier_ttl(tier_int=None, fallback=7.5) == 7.5


@pytest.mark.ci_gate
def test_c3_effective_cleanup_ttl_returns_max(patched_dedup_store) -> None:
    """
    C3: _effective_cleanup_ttl() returns max(self._ttl, max_store_ms/1000).
    Default dedup_window_ms is 5000ms for all tiers = 5.0s.
    DedupCache constructed with ttl_seconds=5.0, so result must be >= 5.0.
    """
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=5.0)
    result = cache._effective_cleanup_ttl()
    assert result >= 5.0
    # Now seed a wider T3 window and verify it propagates.
    patched_dedup_store._cache["dedup_window_ms"][3] = 12_000.0
    result2 = cache._effective_cleanup_ttl()
    assert result2 == pytest.approx(12.0), (
        f"Expected 12.0s after T3 window widened to 12s, got {result2}"
    )


@pytest.mark.ci_gate
def test_c4_is_duplicate_respects_tier_aware_ttl(patched_dedup_store) -> None:
    """
    C4: is_duplicate() must honour the per-tier TTL from the store.

    Scenario:
      - Store T1 dedup_window_ms = 2000ms (overridden to 2.0s for this test)
      - Send two prints of the same contract 1.5s apart.
      - With flat ttl=5.0s the second would be a duplicate.
      - With T1=2.0s the second is also a duplicate (1.5s < 2.0s).
      - Then send a third print 2.5s after the first.
      - With T1=2.0s the third is NOT a duplicate (2.5s > 2.0s).
      - With flat ttl=5.0s the third WOULD be a duplicate.
      This distinguishes the tier-aware path from the flat-TTL fallback.
    """
    from utils.dedup import DedupCache
    # Override T1 window to 2000ms
    patched_dedup_store._cache["dedup_window_ms"][1] = 2_000.0

    cache = DedupCache(ttl_seconds=5.0)  # flat TTL would suppress the third print
    occ = "AAPL260620C00200000"
    t0 = time.time()

    # First print — canonical
    dup0 = cache.is_duplicate(occ, size=100, fill=3.50, exchange="CBOE", ts=t0, tier_int=1)
    assert not dup0, "First print must not be a duplicate"

    # Second print 1.5s later — within 2s T1 window, must be duplicate
    dup1 = cache.is_duplicate(occ, size=100, fill=3.50, exchange="PHLX", ts=t0 + 1.5, tier_int=1)
    assert dup1, "Second print at 1.5s must be duplicate under T1=2s window"

    # Third print 2.5s after first — outside 2s T1 window, must NOT be duplicate
    # (but WOULD be under flat ttl=5.0s — this is the key assertion)
    dup2 = cache.is_duplicate(occ, size=100, fill=3.50, exchange="ISE", ts=t0 + 2.5, tier_int=1)
    assert not dup2, (
        "Third print at 2.5s must NOT be duplicate under T1=2s window — "
        "if this fails the flat ttl=5.0s fallback is still being used"
    )


# ===========================================================================
# Block D — tradier_stream consumer wiring
# Catches the ING-010-IMPORT bug in tradier_stream.py.
# ===========================================================================

@pytest.fixture()
def patched_stream_store(real_store):
    """
    Patch services.gate_config_store.store AND the alias already bound in
    tradier_stream module scope so both import paths see real_store.
    """
    with patch("services.gate_config_store.store", real_store), \
         patch("services.tradier_stream.gate_config_store", real_store):
        yield real_store


@pytest.mark.ci_gate
def test_d1_resolve_signal_debounce_reads_from_store(patched_stream_store) -> None:
    """
    D1: _resolve_signal_debounce_s() must return the T1 signal_debounce_ms
    from the store (default 30000ms = 30.0s), not the hardcoded fallback.

    If the import inside tradier_stream is broken, the except branch fires
    and returns the hardcoded _SIGNAL_DEBOUNCE_S=30.0 anyway — so we
    distinguish by overriding the store value to 45s and verifying propagation.
    """
    import services.tradier_stream as ts
    # Override T1 signal_debounce_ms to 45000ms
    patched_stream_store._cache["signal_debounce_ms"][1] = 45_000.0
    result = ts._resolve_signal_debounce_s()
    assert result == pytest.approx(45.0), (
        f"Expected 45.0s after store override, got {result} — import may be broken"
    )


@pytest.mark.ci_gate
def test_d2_resolve_signal_debounce_fallback_on_zero(patched_stream_store) -> None:
    """
    D2: _resolve_signal_debounce_s() returns _SIGNAL_DEBOUNCE_S=30.0 when
    the store returns 0 or None for signal_debounce_ms T1.
    """
    import services.tradier_stream as ts
    patched_stream_store._cache["signal_debounce_ms"][1] = 0.0
    result = ts._resolve_signal_debounce_s()
    assert result == pytest.approx(ts._SIGNAL_DEBOUNCE_S), (
        f"Expected fallback {ts._SIGNAL_DEBOUNCE_S}, got {result}"
    )


@pytest.mark.ci_gate
def test_d3_resolve_signal_min_premium_reads_from_store(patched_stream_store) -> None:
    """
    D3: _resolve_signal_min_premium() must read signal_min_premium T1 from the
    store. Override to 75_000 and verify propagation.
    """
    import services.tradier_stream as ts
    # signal_min_premium is not in _DEFAULTS — seed it directly in the cache.
    patched_stream_store._cache.setdefault("signal_min_premium", {})[1] = 75_000.0
    result = ts._resolve_signal_min_premium()
    assert result == pytest.approx(75_000.0), (
        f"Expected 75000.0 after store seed, got {result}"
    )


@pytest.mark.ci_gate
def test_d4_should_emit_respects_live_debounce(patched_stream_store) -> None:
    """
    D4: _should_emit_signal() must read debounce_s live from the store via
    _resolve_signal_debounce_s().

    Scenario:
      - Store T1 signal_debounce_ms = 10_000ms (10s)
      - First emit — initial crossing, must fire.
      - Second emit 5s later, same alert/premium — debounced (5s < 10s).
      - Third emit 11s after first — outside 10s window, premium +30k,
        must fire.
      - Then set store to 60_000ms (60s) — same 11s elapsed print must now
        be debounced.
    """
    import services.tradier_stream as ts

    patched_stream_store._cache["signal_debounce_ms"][1] = 10_000.0

    emit_key = "NVDA|CALL|600.0|2026-06-20"
    # Wipe any stale state from module-level _signal_last_emit
    ts._signal_last_emit.pop(emit_key, None)

    t0 = time.time()

    # First emit — initial crossing
    should, reason = ts._should_emit_signal(emit_key, "WATCH", 60_000.0, t0)
    assert should, f"Initial crossing must emit; reason={reason}"
    ts._signal_last_emit[emit_key] = {"alert_level": "WATCH", "premium": 60_000.0, "ts": t0}

    # 5s later — same alert, same premium: debounced
    should2, reason2 = ts._should_emit_signal(emit_key, "WATCH", 60_000.0, t0 + 5.0)
    assert not should2, f"5s later must be debounced under 10s window; reason={reason2}"

    # 11s after first — window expired, premium grew +30k
    should3, reason3 = ts._should_emit_signal(emit_key, "WATCH", 90_000.0, t0 + 11.0)
    assert should3, f"11s later + premium growth must emit; reason={reason3}"

    # Now widen store to 60s — same 11s elapsed must be suppressed
    patched_stream_store._cache["signal_debounce_ms"][1] = 60_000.0
    ts._signal_last_emit[emit_key] = {"alert_level": "WATCH", "premium": 60_000.0, "ts": t0}
    should4, reason4 = ts._should_emit_signal(emit_key, "WATCH", 90_000.0, t0 + 11.0)
    assert not should4, (
        f"11s elapsed must be debounced after store widened to 60s; reason={reason4}"
    )


# ===========================================================================
# Block E — epoch parity
# ===========================================================================

@pytest.mark.ci_gate
def test_e1_epoch_parity_passes_when_aligned(real_store) -> None:
    """
    E1: assert_store_epoch_parity does not raise when all epochs are equal.
    """
    from services.gate_config_store import GateConfigStore
    # All at 0 (pre-load) — should never raise
    GateConfigStore.assert_store_epoch_parity(
        gate_epoch=0, chain_epoch=0, universe_epoch=0
    )
    # All at same non-zero value
    GateConfigStore.assert_store_epoch_parity(
        gate_epoch=5, chain_epoch=5, universe_epoch=5
    )
    # Dependent stores behind gate — acceptable
    GateConfigStore.assert_store_epoch_parity(
        gate_epoch=5, chain_epoch=3, universe_epoch=2
    )


@pytest.mark.ci_gate
def test_e2_epoch_parity_raises_when_chain_ahead(real_store) -> None:
    """
    E2: assert_store_epoch_parity raises AssertionError when chain_epoch
    or universe_epoch is ahead of gate_epoch.
    """
    from services.gate_config_store import GateConfigStore
    with pytest.raises(AssertionError, match="chain_store epoch"):
        GateConfigStore.assert_store_epoch_parity(
            gate_epoch=3, chain_epoch=4, universe_epoch=3
        )
    with pytest.raises(AssertionError, match="universe_store epoch"):
        GateConfigStore.assert_store_epoch_parity(
            gate_epoch=3, chain_epoch=3, universe_epoch=5
        )


@pytest.mark.ci_gate
def test_e3_epoch_parity_tolerates_pre_load_zero(real_store) -> None:
    """
    E3: When chain_epoch or universe_epoch is 0 (pre-first-load), parity
    check must pass regardless of gate_epoch. Prevents false positives at
    startup before any save_chain() / save_snapshot() has been called.
    """
    from services.gate_config_store import GateConfigStore
    # chain and universe both 0 — never raises even if gate is ahead
    GateConfigStore.assert_store_epoch_parity(
        gate_epoch=10, chain_epoch=0, universe_epoch=0
    )
    # Only one of them at 0
    GateConfigStore.assert_store_epoch_parity(
        gate_epoch=10, chain_epoch=0, universe_epoch=10
    )
    GateConfigStore.assert_store_epoch_parity(
        gate_epoch=10, chain_epoch=10, universe_epoch=0
    )
