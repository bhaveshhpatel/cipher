"""
test_gate_config_store.py — ING-010: Unit tests for GateConfigStore.

Test matrix covers TGC-1 through TGC-10 acceptance criteria from
docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md ING-010 story.

All DB calls are mocked via unittest.mock — no live Supabase dependency.

Interface under test
--------------------
  GateConfigStore.__init__()  — seeds _cache from _DEFAULTS
  GateConfigStore.get()       — O(1) thread-safe read, tier-fallback to T3
  GateConfigStore.load()      — async; reads Supabase, advances epoch
  GateConfigStore.update()    — async; validates, PATCHes DB, updates cache
  store                       — module-level singleton (exported as 'store')

NOT tested here (no such methods on GateConfigStore):
  get_all(), _cast(), _loaded flag
"""
from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.gate_config_store import (
    GateConfigStore,
    _BOUNDS,
    _DEFAULTS,
    _VALID_GATES,
    _VALID_TIERS,
    store,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(url: str = "https://fake.supabase.co", key: str = "fake-key") -> GateConfigStore:
    """Return a GateConfigStore wired to fake Supabase credentials."""
    s = GateConfigStore()
    s._supabase_url = url
    s._supabase_key = key
    return s


def make_no_db_store() -> GateConfigStore:
    """Return a GateConfigStore in no-DB mode (empty url/key strings)."""
    s = GateConfigStore()
    s._supabase_url = ""
    s._supabase_key = ""
    return s


def mock_http_get(rows: list[dict], status: int = 200):
    """
    Return an AsyncClient context-manager mock for load().
    When status != 200, resp.raise_for_status() raises HTTPStatusError.
    """
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = rows
    if status != 200:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


def mock_http_patch_audit(patch_status: int = 204, audit_status: int = 201):
    """Return an AsyncClient mock for update() (PATCH + POST audit)."""
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


def _off_hours_dt() -> datetime.datetime:
    """Saturday 10:00 UTC — outside market hours."""
    return datetime.datetime(2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)


def _market_hours_dt() -> datetime.datetime:
    """Wednesday 14:00 UTC — inside market hours."""
    return datetime.datetime(2026, 5, 6, 14, 0, 0, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# TGC-8: Cold start — no Supabase configured — fallback defaults load cleanly
# ---------------------------------------------------------------------------

class TestColdStartFallback:
    def test_new_store_seeds_all_defaults(self):
        """Every gate+tier in _DEFAULTS must be reachable via get()."""
        s = GateConfigStore()
        for gate, tiers in _DEFAULTS.items():
            for tier, expected in tiers.items():
                assert s.get(gate, tier) == expected, (
                    f"get({gate!r}, {tier}) expected {expected}, got {s.get(gate, tier)}"
                )

    def test_new_store_epoch_is_zero(self):
        s = GateConfigStore()
        assert s.epoch == 0

    @pytest.mark.asyncio
    async def test_load_no_db_mode_is_noop(self):
        """load() in no-DB mode returns without touching in-memory cache."""
        s = make_no_db_store()
        epoch_before = s.epoch
        await s.load()
        # epoch unchanged — no-db load() returns early without incrementing
        assert s.epoch == epoch_before

    @pytest.mark.asyncio
    async def test_load_no_db_preserves_defaults(self):
        s = make_no_db_store()
        await s.load()
        for gate, tiers in _DEFAULTS.items():
            for tier, expected in tiers.items():
                assert s.get(gate, tier) == expected


# ---------------------------------------------------------------------------
# load() — DB-backed startup
# ---------------------------------------------------------------------------

class TestLoad:
    @pytest.mark.asyncio
    async def test_load_overwrites_defaults_from_db(self):
        s = make_store()
        rows = [{"gate_name": "min_premium", "tier": 3, "value": 7_500.0}]
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await s.load()
        assert s.get("min_premium", 3) == 7_500.0

    @pytest.mark.asyncio
    async def test_load_increments_epoch(self):
        s = make_store()
        rows = [{"gate_name": "min_premium", "tier": 1, "value": 25_000.0}]
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            assert s.epoch == 0
            await s.load()
            assert s.epoch == 1

    @pytest.mark.asyncio
    async def test_load_non200_preserves_defaults(self):
        """
        load() calls raise_for_status() which raises HTTPStatusError on 500.
        The in-memory cache is never mutated so defaults are preserved.
        """
        s = make_store()
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get([], status=500)):
            # load() allows the exception to propagate — caller handles it
            with pytest.raises(httpx.HTTPStatusError):
                await s.load()
        # Cache untouched — defaults still intact
        assert s.get("min_premium", 1) == 25_000.0

    @pytest.mark.asyncio
    async def test_load_network_exception_propagates(self):
        """Any network error propagates out of load()."""
        s = make_store()
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=Exception("timeout"))
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=client):
            with pytest.raises(Exception, match="timeout"):
                await s.load()
        # Defaults preserved because cache was never touched
        assert s.get("min_premium", 2) == 15_000.0

    @pytest.mark.asyncio
    async def test_load_tier99_row_stored_but_get_falls_back_to_t3(self):
        """
        load() stores tier-99 as-is; get(gate, 99) falls back to T3 value.
        This tests the get() fallback, not tier rejection in load().
        """
        s = make_store()
        rows = [{"gate_name": "min_premium", "tier": 99, "value": 999.0}]
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await s.load()
        # tier 99 stored but get(99) maps to T3 — still 10_000 default (row only set 99)
        assert s.get("min_premium", 3) == 10_000.0
        # Unknown tier get() returns the T3 value
        assert s.get("min_premium", 99) == s.get("min_premium", 3)

    @pytest.mark.asyncio
    async def test_load_multiple_rows_applied(self):
        s = make_store()
        rows = [
            {"gate_name": "min_premium", "tier": 1, "value": 30_000.0},
            {"gate_name": "min_premium", "tier": 2, "value": 20_000.0},
            {"gate_name": "dedup_window_ms", "tier": 1, "value": 3_000.0},
        ]
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await s.load()
        assert s.get("min_premium", 1) == 30_000.0
        assert s.get("min_premium", 2) == 20_000.0
        assert s.get("dedup_window_ms", 1) == 3_000.0

    @pytest.mark.asyncio
    async def test_load_with_bounds_columns_updates_bounds_cache(self):
        """When DB rows include min_value/max_value, _bounds_cache is updated."""
        s = make_store()
        rows = [{
            "gate_name": "min_premium",
            "tier": 1,
            "value": 25_000.0,
            "min_value": 2_000.0,
            "max_value": 400_000.0,
        }]
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await s.load()
        assert s._bounds_cache["min_premium"] == (2_000.0, 400_000.0)


# ---------------------------------------------------------------------------
# TGC-5: get() — reads, tier-fallback, unknown gate
# ---------------------------------------------------------------------------

class TestGet:
    def test_valid_tier_returns_correct_value(self):
        s = GateConfigStore()
        assert s.get("min_premium", 1) == 25_000.0
        assert s.get("min_premium", 2) == 15_000.0
        assert s.get("min_premium", 3) == 10_000.0

    def test_unknown_tier_falls_back_to_t3(self):
        s = GateConfigStore()
        assert s.get("min_premium", 99) == s.get("min_premium", 3)

    def test_tier_zero_falls_back_to_t3(self):
        s = GateConfigStore()
        assert s.get("signal_debounce_ms", 0) == s.get("signal_debounce_ms", 3)

    def test_unknown_gate_returns_zero(self):
        """get() returns 0.0 for completely unknown gate names."""
        s = GateConfigStore()
        assert s.get("nonexistent_gate", 1) == 0.0

    def test_require_oi_all_tiers_default_zero(self):
        """require_oi defaults to 0.0 (float), not True/False."""
        s = GateConfigStore()
        for tier in _VALID_TIERS:
            assert s.get("require_oi", tier) == 0.0

    def test_dte_multiplier_tier_ordering(self):
        s = GateConfigStore()
        # T1 > T2 > T3
        assert s.get("dte_floor_multiplier", 1) > s.get("dte_floor_multiplier", 2)
        assert s.get("dte_floor_multiplier", 2) > s.get("dte_floor_multiplier", 3)

    def test_signal_debounce_ms_tier_ordering(self):
        s = GateConfigStore()
        # T1 debounce < T2 < T3 (T1 is most active, shortest cooldown)
        assert s.get("signal_debounce_ms", 1) < s.get("signal_debounce_ms", 2)
        assert s.get("signal_debounce_ms", 2) < s.get("signal_debounce_ms", 3)

    def test_debounce_ms_alias_matches_signal_debounce_ms(self):
        """
        'debounce_ms' is a separate gate entry in _DEFAULTS seeded with the
        same T1/T2/T3 values as signal_debounce_ms. Both must be reachable.
        """
        s = GateConfigStore()
        for tier in _VALID_TIERS:
            assert s.get("debounce_ms", tier) == s.get("signal_debounce_ms", tier)

    def test_dedup_window_ms_all_tiers_equal(self):
        s = GateConfigStore()
        # dedup_window_ms defaults to 5000 for all tiers
        assert s.get("dedup_window_ms", 1) == s.get("dedup_window_ms", 2) == s.get("dedup_window_ms", 3)


# ---------------------------------------------------------------------------
# TGC-3: Bounds validation — invalid values rejected with ValueError
# ---------------------------------------------------------------------------

class TestBoundsValidation:
    @pytest.mark.asyncio
    async def test_min_premium_below_1000_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await s.update("min_premium", 1, 500)

    @pytest.mark.asyncio
    async def test_min_premium_above_500k_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await s.update("min_premium", 1, 600_000)

    @pytest.mark.asyncio
    async def test_dte_multiplier_below_01_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await s.update("dte_floor_multiplier", 2, 0.05)

    @pytest.mark.asyncio
    async def test_dte_multiplier_above_5_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await s.update("dte_floor_multiplier", 1, 6.0)

    @pytest.mark.asyncio
    async def test_dedup_window_ms_below_500_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await s.update("dedup_window_ms", 1, 100)

    @pytest.mark.asyncio
    async def test_signal_debounce_ms_above_600000_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await s.update("signal_debounce_ms", 3, 700_000)

    @pytest.mark.asyncio
    async def test_unknown_gate_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="Unknown gate"):
            await s.update("fake_gate", 1, 100)

    @pytest.mark.asyncio
    async def test_invalid_tier_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="Invalid tier"):
            await s.update("min_premium", 99, 10_000)

    @pytest.mark.asyncio
    async def test_require_oi_above_1_raises(self):
        s = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await s.update("require_oi", 1, 2.0)


# ---------------------------------------------------------------------------
# TGC-4: Market-hours guard
# ---------------------------------------------------------------------------

class TestMarketHoursGuard:
    @pytest.mark.asyncio
    async def test_market_hours_no_confirm_raises(self):
        s = make_store()
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _market_hours_dt()
            mock_dt.time = datetime.time
            with pytest.raises(ValueError, match="Market is currently open"):
                await s.update("min_premium", 3, 12_000, confirm_market_hours=False)

    @pytest.mark.asyncio
    async def test_market_hours_with_confirm_proceeds(self):
        s = make_store()
        client_mock = mock_http_patch_audit()
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _market_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                result = await s.update("min_premium", 3, 12_000, confirm_market_hours=True)
        assert result["new_value"] == 12_000

    @pytest.mark.asyncio
    async def test_outside_market_hours_no_confirm_proceeds(self):
        """Off-hours update with confirm_market_hours=False must NOT raise."""
        s = make_store()
        client_mock = mock_http_patch_audit()
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                result = await s.update("min_premium", 2, 18_000, confirm_market_hours=False)
        assert result["new_value"] == 18_000


# ---------------------------------------------------------------------------
# TGC-1/TGC-2: update() — hot-reload behaviour
# ---------------------------------------------------------------------------

class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_changes_in_memory_value(self):
        s = make_store()
        client_mock = mock_http_patch_audit()
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                await s.update("min_premium", 3, 12_000)
        assert s.get("min_premium", 3) == 12_000

    @pytest.mark.asyncio
    async def test_update_returns_old_and_new_values(self):
        s = make_store()
        client_mock = mock_http_patch_audit()
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                result = await s.update("min_premium", 3, 12_000)
        assert result["old_value"] == 10_000.0
        assert result["new_value"] == 12_000
        assert result["gate_name"] == "min_premium"
        assert result["tier"] == 3

    @pytest.mark.asyncio
    async def test_update_increments_epoch(self):
        s = make_store()
        epoch_before = s.epoch
        client_mock = mock_http_patch_audit()
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                await s.update("signal_debounce_ms", 1, 45_000)
        assert s.epoch == epoch_before + 1

    @pytest.mark.asyncio
    async def test_update_db_patch_failure_raises_runtime_error(self):
        s = make_store()
        client_mock = mock_http_patch_audit(patch_status=500)
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                with pytest.raises(RuntimeError, match="DB PATCH failed"):
                    await s.update("min_premium", 1, 30_000)

    @pytest.mark.asyncio
    async def test_update_db_patch_failure_does_not_mutate_cache(self):
        """If DB PATCH fails, the in-memory cache must NOT be updated."""
        s = make_store()
        original_value = s.get("min_premium", 1)
        client_mock = mock_http_patch_audit(patch_status=500)
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                with pytest.raises(RuntimeError):
                    await s.update("min_premium", 1, 30_000)
        assert s.get("min_premium", 1) == original_value

    @pytest.mark.asyncio
    async def test_update_no_db_mode_updates_in_memory_only(self):
        s = make_no_db_store()
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            result = await s.update("min_premium", 2, 20_000)
        assert result["new_value"] == 20_000
        assert s.get("min_premium", 2) == 20_000
        assert "_note" in result

    @pytest.mark.asyncio
    async def test_update_no_db_mode_increments_epoch(self):
        s = make_no_db_store()
        epoch_before = s.epoch
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            await s.update("min_premium", 2, 20_000)
        assert s.epoch == epoch_before + 1

    @pytest.mark.asyncio
    async def test_update_audit_failure_is_non_fatal(self):
        """Audit POST failure must not raise — update() result still returned."""
        s = make_store()
        client_mock = mock_http_patch_audit(patch_status=204, audit_status=500)
        with patch("services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = _off_hours_dt()
            mock_dt.time = datetime.time
            with patch("services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                result = await s.update("dedup_window_ms", 2, 8_000)
        assert result["new_value"] == 8_000
        assert s.get("dedup_window_ms", 2) == 8_000


# ---------------------------------------------------------------------------
# Epoch behaviour
# ---------------------------------------------------------------------------

class TestEpoch:
    def test_initial_epoch_is_zero(self):
        s = GateConfigStore()
        assert s.epoch == 0

    @pytest.mark.asyncio
    async def test_load_advances_epoch(self):
        s = make_store()
        rows = [{"gate_name": "min_premium", "tier": 1, "value": 25_000.0}]
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await s.load()
        assert s.epoch == 1

    @pytest.mark.asyncio
    async def test_load_called_twice_epoch_is_two(self):
        s = make_store()
        rows = [{"gate_name": "min_premium", "tier": 1, "value": 25_000.0}]
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await s.load()
        with patch("services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await s.load()
        assert s.epoch == 2

    @pytest.mark.asyncio
    async def test_no_db_load_does_not_advance_epoch(self):
        s = make_no_db_store()
        await s.load()
        assert s.epoch == 0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestModuleSingleton:
    def test_singleton_exists(self):
        assert store is not None
        assert isinstance(store, GateConfigStore)

    def test_singleton_seeds_all_defaults(self):
        for gate, tiers in _DEFAULTS.items():
            for tier, expected in tiers.items():
                assert store.get(gate, tier) == expected

    def test_singleton_has_valid_gates(self):
        for gate in _VALID_GATES:
            # Every valid gate must return a numeric value for T3
            val = store.get(gate, 3)
            assert isinstance(val, (int, float))
