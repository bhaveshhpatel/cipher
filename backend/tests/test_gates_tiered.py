"""
test_gates_tiered.py — Per-tier permutation tests across all gates.

Exhaustively exercises every (gate, tier) combination drawn from
``_BOUNDS`` × ``_VALID_TIERS`` so that new gates automatically gain
coverage without touching this file.

Test classes
------------
TestFallbackPerTier       — cold-start fallbacks are present & in-bounds for every combo
TestGetPerTier            — get() returns typed, in-bounds values for every combo
TestBoundsEnforcementPerTier — below-min / above-max raises ValueError for every combo
TestUpdateRoundtripPerTier   — off-hours update persists & is reflected by get() per combo
TestEpochMonotonicPerTier    — epoch increments once per update across every gate×tier
TestUnknownTierFallbackPerTier — invalid tier always resolves to _SAFE_DEFAULT_TIER value
"""
from __future__ import annotations

import datetime
import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.gate_config_store import (
    GateConfigStore,
    _BOUNDS,
    _FALLBACK,
    _SAFE_DEFAULT_TIER,
    _VALID_TIERS,
    gate_config_store,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ALL_GATES: list[str] = sorted(_BOUNDS.keys())
_ALL_TIERS: list[int] = sorted(_VALID_TIERS)
_GATE_TIER_PAIRS: list[tuple[str, int]] = list(itertools.product(_ALL_GATES, _ALL_TIERS))

# Saturdays are reliably off market hours; reuse a fixed instant across tests.
_OFF_HOURS_DT = datetime.datetime(2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)
_MARKET_DT = datetime.datetime(2026, 5, 6, 14, 0, 0, tzinfo=datetime.timezone.utc)


def _fresh_store() -> GateConfigStore:
    """Return an isolated GateConfigStore with no Supabase wiring."""
    store = GateConfigStore()
    store._supabase_url = None
    store._supabase_key = None
    return store


def _wired_store() -> GateConfigStore:
    """Return a store wired to a fake Supabase URL so update() will try the DB path."""
    store = GateConfigStore()
    store._supabase_url = "https://fake.supabase.co"
    store._supabase_key = "fake-key"
    return store


def _mock_patch_audit(patch_status: int = 204, audit_status: int = 201):
    patch_resp = MagicMock()
    patch_resp.status_code = patch_status
    audit_resp = MagicMock()
    audit_resp.status_code = audit_status
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.patch = AsyncMock(return_value=patch_resp)
    client.post = AsyncMock(return_value=audit_resp)
    return client


def _mid_valid_value(gate: str) -> Any:
    """Return a value that is strictly inside [lo, hi] for the given gate."""
    lo, hi, cast = _BOUNDS[gate]
    if cast is bool:
        return True
    mid = (lo + hi) / 2.0
    return cast(mid) if cast is not bool else bool(mid)


def _below_min(gate: str) -> Any:
    """Return a value strictly below the gate's minimum bound."""
    lo, _hi, cast = _BOUNDS[gate]
    if cast is bool:
        pytest.skip("bool gate has no numeric underflow")
    return cast(lo - 1)


def _above_max(gate: str) -> Any:
    """Return a value strictly above the gate's maximum bound."""
    _lo, hi, cast = _BOUNDS[gate]
    if cast is bool:
        pytest.skip("bool gate has no numeric overflow")
    return cast(hi + 1)


# ---------------------------------------------------------------------------
# TestFallbackPerTier
# ---------------------------------------------------------------------------

class TestFallbackPerTier:
    """Cold-start: every (gate, tier) in _FALLBACK is present and within bounds."""

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    def test_fallback_exists_for_gate_tier(self, gate: str, tier: int) -> None:
        store = _fresh_store()
        # get() must not raise; it either returns the fallback or the safe-default-tier value
        value = store.get(gate, tier)
        assert value is not None, f"get({gate!r}, {tier}) returned None"

    @pytest.mark.parametrize("gate,tier", [(g, t) for (g, t) in _FALLBACK])
    def test_fallback_value_matches_constant(self, gate: str, tier: int) -> None:
        store = _fresh_store()
        assert store.get(gate, tier) == _FALLBACK[(gate, tier)]

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    def test_fallback_within_bounds(self, gate: str, tier: int) -> None:
        lo, hi, cast = _BOUNDS[gate]
        store = _fresh_store()
        value = store.get(gate, tier)
        if cast is bool:
            assert isinstance(value, bool)
        else:
            assert lo <= value <= hi, (
                f"Fallback {value!r} for ({gate!r}, {tier}) out of bounds [{lo}, {hi}]"
            )


# ---------------------------------------------------------------------------
# TestGetPerTier
# ---------------------------------------------------------------------------

class TestGetPerTier:
    """get() semantics across all (gate, tier) permutations."""

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    def test_get_returns_correct_type(self, gate: str, tier: int) -> None:
        _lo, _hi, cast = _BOUNDS[gate]
        store = _fresh_store()
        value = store.get(gate, tier)
        if cast is bool:
            assert isinstance(value, bool), f"Expected bool for ({gate!r}, {tier})"
        elif cast is int:
            assert isinstance(value, int), f"Expected int for ({gate!r}, {tier})"
        elif cast is float:
            assert isinstance(value, (int, float)), f"Expected numeric for ({gate!r}, {tier})"

    @pytest.mark.parametrize("gate", _ALL_GATES)
    def test_get_unknown_tier_returns_safe_default(self, gate: str) -> None:
        store = _fresh_store()
        safe_value = store.get(gate, _SAFE_DEFAULT_TIER)
        for bad_tier in (0, -1, 99, 999):
            assert store.get(gate, bad_tier) == safe_value, (
                f"get({gate!r}, {bad_tier}) should equal safe-default-tier value {safe_value!r}"
            )

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    def test_get_idempotent(self, gate: str, tier: int) -> None:
        store = _fresh_store()
        first = store.get(gate, tier)
        second = store.get(gate, tier)
        assert first == second, f"get({gate!r}, {tier}) is not idempotent"

    @pytest.mark.parametrize("gate", _ALL_GATES)
    def test_get_all_tiers_distinct_or_ordered(self, gate: str) -> None:
        """Values across tiers must either be strictly ordered or all equal (bool gates)."""
        store = _fresh_store()
        _lo, _hi, cast = _BOUNDS[gate]
        values = [store.get(gate, t) for t in _ALL_TIERS]
        if cast is bool:
            # bool gates are allowed to be uniform
            assert all(isinstance(v, bool) for v in values)
        else:
            # Numeric gates must be monotone (either non-increasing or non-decreasing)
            non_decreasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
            non_increasing = all(values[i] >= values[i + 1] for i in range(len(values) - 1))
            assert non_decreasing or non_increasing, (
                f"Tier values for {gate!r} are neither monotone ascending nor descending: {values}"
            )


# ---------------------------------------------------------------------------
# TestBoundsEnforcementPerTier
# ---------------------------------------------------------------------------

class TestBoundsEnforcementPerTier:
    """update() must reject out-of-bounds values for every (gate, tier)."""

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_below_min_raises_value_error(self, gate: str, tier: int) -> None:
        lo, _hi, cast = _BOUNDS[gate]
        if cast is bool:
            pytest.skip("bool gate has no numeric underflow")
        store = _wired_store()
        bad_value = cast(lo - 1)
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update(gate, tier, bad_value)

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_above_max_raises_value_error(self, gate: str, tier: int) -> None:
        _lo, hi, cast = _BOUNDS[gate]
        if cast is bool:
            pytest.skip("bool gate has no numeric overflow")
        store = _wired_store()
        bad_value = cast(hi + 1)
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update(gate, tier, bad_value)

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_valid_mid_value_does_not_raise_bounds(self, gate: str, tier: int) -> None:
        """A value inside bounds must not raise ValueError for bounds reason."""
        lo, hi, cast = _BOUNDS[gate]
        if cast is bool:
            mid_val: Any = True
        else:
            mid_val = cast((lo + hi) / 2.0)
        store = _fresh_store()  # no DB, so update() is in-memory only
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _OFF_HOURS_DT
            mock_dt.time = datetime.time
            # Should not raise ValueError for bounds
            try:
                await store.update(gate, tier, mid_val)
            except ValueError as exc:
                if "outside allowed bounds" in str(exc):
                    pytest.fail(
                        f"update({gate!r}, {tier}, {mid_val!r}) raised bounds error: {exc}"
                    )

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_invalid_tier_always_raises(self, gate: str, tier: int) -> None:
        """Regardless of gate, an invalid tier must raise ValueError."""
        store = _wired_store()
        lo, hi, cast = _BOUNDS[gate]
        mid_val = True if cast is bool else cast((lo + hi) / 2.0)
        with pytest.raises(ValueError, match="Invalid tier"):
            await store.update(gate, 99, mid_val)


# ---------------------------------------------------------------------------
# TestUpdateRoundtripPerTier
# ---------------------------------------------------------------------------

class TestUpdateRoundtripPerTier:
    """update() → get() round-trip: every (gate, tier) must persist the new value."""

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_update_persists_new_value(self, gate: str, tier: int) -> None:
        lo, hi, cast = _BOUNDS[gate]
        if cast is bool:
            new_val: Any = True
        else:
            new_val = cast((lo + hi) / 2.0)
        store = _fresh_store()
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _OFF_HOURS_DT
            mock_dt.time = datetime.time
            await store.update(gate, tier, new_val)
        assert store.get(gate, tier) == new_val, (
            f"After update({gate!r}, {tier}, {new_val!r}), get() returned "
            f"{store.get(gate, tier)!r}"
        )

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_update_returns_old_value_in_result(self, gate: str, tier: int) -> None:
        lo, hi, cast = _BOUNDS[gate]
        if cast is bool:
            new_val = True
        else:
            new_val = cast((lo + hi) / 2.0)
        store = _fresh_store()
        old_val = store.get(gate, tier)
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _OFF_HOURS_DT
            mock_dt.time = datetime.time
            result = await store.update(gate, tier, new_val)
        assert result["old_value"] == old_val, (
            f"update({gate!r}, {tier}) old_value={result['old_value']!r} != {old_val!r}"
        )
        assert result["new_value"] == new_val
        assert result["gate_name"] == gate
        assert result["tier"] == tier

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_update_does_not_bleed_into_other_tiers(self, gate: str, tier: int) -> None:
        """Updating tier T must not change the value for any other tier of the same gate."""
        lo, hi, cast = _BOUNDS[gate]
        if cast is bool:
            new_val = True
        else:
            # Use a distinct value: max of valid range
            new_val = cast(hi)
        store = _fresh_store()
        snapshot_before = {t: store.get(gate, t) for t in _ALL_TIERS if t != tier}
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _OFF_HOURS_DT
            mock_dt.time = datetime.time
            await store.update(gate, tier, new_val)
        for other_tier, before_val in snapshot_before.items():
            after_val = store.get(gate, other_tier)
            assert after_val == before_val, (
                f"update({gate!r}, {tier}) unexpectedly changed ({gate!r}, {other_tier}): "
                f"{before_val!r} → {after_val!r}"
            )

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_update_does_not_bleed_into_other_gates(self, gate: str, tier: int) -> None:
        """Updating one gate must not alter any other gate's values."""
        lo, hi, cast = _BOUNDS[gate]
        new_val: Any = True if cast is bool else cast((lo + hi) / 2.0)
        store = _fresh_store()
        snapshot_before = {
            (g, t): store.get(g, t)
            for g in _ALL_GATES if g != gate
            for t in _ALL_TIERS
        }
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _OFF_HOURS_DT
            mock_dt.time = datetime.time
            await store.update(gate, tier, new_val)
        for (g, t), before_val in snapshot_before.items():
            after_val = store.get(g, t)
            assert after_val == before_val, (
                f"update({gate!r}, {tier}) bled into ({g!r}, {t}): "
                f"{before_val!r} → {after_val!r}"
            )


# ---------------------------------------------------------------------------
# TestEpochMonotonicPerTier
# ---------------------------------------------------------------------------

class TestEpochMonotonicPerTier:
    """Epoch must increment by exactly 1 for each successful update."""

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_epoch_increments_once_per_update(self, gate: str, tier: int) -> None:
        lo, hi, cast = _BOUNDS[gate]
        new_val: Any = True if cast is bool else cast((lo + hi) / 2.0)
        store = _fresh_store()
        epoch_before = store.epoch
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _OFF_HOURS_DT
            mock_dt.time = datetime.time
            await store.update(gate, tier, new_val)
        assert store.epoch == epoch_before + 1, (
            f"Epoch after update({gate!r}, {tier}) should be {epoch_before + 1}, "
            f"got {store.epoch}"
        )

    @pytest.mark.parametrize("gate", _ALL_GATES)
    @pytest.mark.asyncio
    async def test_epoch_accumulates_across_tiers(self, gate: str) -> None:
        """N sequential updates across all tiers must advance epoch by N."""
        lo, hi, cast = _BOUNDS[gate]
        new_val: Any = True if cast is bool else cast((lo + hi) / 2.0)
        store = _fresh_store()
        epoch_start = store.epoch
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _OFF_HOURS_DT
            mock_dt.time = datetime.time
            for tier in _ALL_TIERS:
                await store.update(gate, tier, new_val)
        assert store.epoch == epoch_start + len(_ALL_TIERS)

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_failed_update_does_not_increment_epoch(self, gate: str, tier: int) -> None:
        lo, _hi, cast = _BOUNDS[gate]
        if cast is bool:
            pytest.skip("bool gate has no numeric underflow")
        store = _fresh_store()
        epoch_before = store.epoch
        with pytest.raises(ValueError):
            await store.update(gate, tier, cast(lo - 1))
        assert store.epoch == epoch_before, (
            f"Epoch should not change after failed update({gate!r}, {tier})"
        )


# ---------------------------------------------------------------------------
# TestUnknownTierFallbackPerTier
# ---------------------------------------------------------------------------

class TestUnknownTierFallbackPerTier:
    """get() with any invalid tier must always resolve to the safe-default-tier value."""

    @pytest.mark.parametrize("gate", _ALL_GATES)
    @pytest.mark.parametrize("bad_tier", [0, -1, 4, 99, 1000, -999])
    def test_invalid_tier_resolves_to_safe_default(self, gate: str, bad_tier: int) -> None:
        store = _fresh_store()
        expected = store.get(gate, _SAFE_DEFAULT_TIER)
        got = store.get(gate, bad_tier)
        assert got == expected, (
            f"get({gate!r}, {bad_tier}) → {got!r}, expected safe-default "
            f"(tier={_SAFE_DEFAULT_TIER}) value {expected!r}"
        )

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    def test_valid_tier_does_not_resolve_to_safe_default_incorrectly(
        self, gate: str, tier: int
    ) -> None:
        """
        For gates where tier values differ, a valid tier must not silently return the
        safe-default-tier value when tier != _SAFE_DEFAULT_TIER.

        Bool gates (uniform across tiers) are skipped since all values are equal.
        """
        _lo, _hi, cast = _BOUNDS[gate]
        if cast is bool:
            pytest.skip("bool gate is uniform across tiers")
        store = _fresh_store()
        safe_val = store.get(gate, _SAFE_DEFAULT_TIER)
        actual_val = store.get(gate, tier)
        if tier == _SAFE_DEFAULT_TIER:
            assert actual_val == safe_val
        else:
            # Only assert they differ if _FALLBACK has distinct values for these tiers
            key_safe = (gate, _SAFE_DEFAULT_TIER)
            key_this = (gate, tier)
            if key_safe in _FALLBACK and key_this in _FALLBACK:
                if _FALLBACK[key_safe] != _FALLBACK[key_this]:
                    assert actual_val != safe_val, (
                        f"get({gate!r}, {tier}) = {actual_val!r} incorrectly equals "
                        f"safe-default value {safe_val!r} even though fallbacks differ"
                    )


# ---------------------------------------------------------------------------
# TestMarketHoursGuardPerTier
# ---------------------------------------------------------------------------

class TestMarketHoursGuardPerTier:
    """Market-hours guard must fire for every (gate, tier) during market hours."""

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_market_hours_blocks_update_without_confirm(
        self, gate: str, tier: int
    ) -> None:
        lo, hi, cast = _BOUNDS[gate]
        new_val: Any = True if cast is bool else cast((lo + hi) / 2.0)
        store = _wired_store()
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _MARKET_DT
            mock_dt.time = datetime.time
            with pytest.raises(ValueError, match="Market is currently open"):
                await store.update(gate, tier, new_val, confirm_market_hours=False)

    @pytest.mark.parametrize("gate,tier", _GATE_TIER_PAIRS)
    @pytest.mark.asyncio
    async def test_market_hours_allows_update_with_confirm(
        self, gate: str, tier: int
    ) -> None:
        lo, hi, cast = _BOUNDS[gate]
        new_val: Any = True if cast is bool else cast((lo + hi) / 2.0)
        store = _fresh_store()  # no DB, so avoids HTTP
        with patch(
            "backend.services.gate_config_store.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = _MARKET_DT
            mock_dt.time = datetime.time
            result = await store.update(gate, tier, new_val, confirm_market_hours=True)
        assert result["new_value"] == new_val
