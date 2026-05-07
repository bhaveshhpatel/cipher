"""
test_gate_config_store.py — ING-010: Unit tests for GateConfigStore.

Test matrix covers TGC-1 through TGC-10 acceptance criteria from
docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md ING-010 story.

All DB calls are mocked via unittest.mock — no live Supabase dependency.
"""
from __future__ import annotations

import asyncio
import datetime
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
# Helpers
# ---------------------------------------------------------------------------

def make_store(url: str = "https://fake.supabase.co", key: str = "fake-key") -> GateConfigStore:
    store = GateConfigStore()
    store._supabase_url = url
    store._supabase_key = key
    return store


def mock_http_get(rows: list[dict], status: int = 200):
    """Return a context-manager mock that yields a GET response with given rows."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = rows
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


def mock_http_patch_audit(patch_status: int = 204, audit_status: int = 201):
    """Return a client mock for update() that handles PATCH + POST."""
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


# ---------------------------------------------------------------------------
# TGC-8: Cold start — no Supabase configured — fallback defaults load cleanly
# ---------------------------------------------------------------------------

class TestColdStartFallback:
    def test_new_store_has_all_fallback_values(self):
        store = GateConfigStore()
        for (gate, tier), expected in _FALLBACK.items():
            assert store.get(gate, tier) == expected

    def test_new_store_not_loaded_flag(self):
        store = GateConfigStore()
        assert store._loaded is False

    @pytest.mark.asyncio
    async def test_load_no_supabase_config_sets_loaded(self):
        store = GateConfigStore()
        store._supabase_url = None
        store._supabase_key = None
        await store.load()
        assert store._loaded is True

    @pytest.mark.asyncio
    async def test_load_no_supabase_config_preserves_fallbacks(self):
        store = GateConfigStore()
        store._supabase_url = None
        store._supabase_key = None
        await store.load()
        for (gate, tier), expected in _FALLBACK.items():
            assert store.get(gate, tier) == expected


# ---------------------------------------------------------------------------
# load() — DB-backed startup
# ---------------------------------------------------------------------------

class TestLoad:
    @pytest.mark.asyncio
    async def test_load_overwrites_defaults_from_db(self):
        store = make_store()
        rows = [
            {"gate_name": "min_premium", "tier": 3, "value": "7500"},
        ]
        with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await store.load()
        assert store.get("min_premium", 3) == 7_500

    @pytest.mark.asyncio
    async def test_load_non200_falls_back(self):
        store = make_store()
        with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get([], status=500)):
            await store.load()
        # Fallback values still present
        assert store.get("min_premium", 1) == 25_000

    @pytest.mark.asyncio
    async def test_load_db_exception_falls_back(self):
        store = make_store()
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=Exception("timeout"))
        with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=client):
            await store.load()
        assert store._loaded is True
        assert store.get("min_premium", 2) == 15_000

    @pytest.mark.asyncio
    async def test_load_increments_epoch(self):
        store = make_store()
        rows = [{"gate_name": "min_premium", "tier": 1, "value": "25000"}]
        with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            assert store.epoch == 0
            await store.load()
            assert store.epoch == 1

    @pytest.mark.asyncio
    async def test_load_skips_invalid_tier_rows(self):
        store = make_store()
        rows = [
            {"gate_name": "min_premium", "tier": 99, "value": "999"},
        ]
        with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=mock_http_get(rows)):
            await store.load()
        # tier 99 is invalid — fallback for tier 3 should remain 10_000
        assert store.get("min_premium", 3) == 10_000


# ---------------------------------------------------------------------------
# TGC-5: get() — unknown tier defaults to T3
# ---------------------------------------------------------------------------

class TestGet:
    def test_valid_tier_returns_correct_value(self):
        store = GateConfigStore()
        assert store.get("min_premium", 1) == 25_000
        assert store.get("min_premium", 2) == 15_000
        assert store.get("min_premium", 3) == 10_000

    def test_unknown_tier_returns_safe_default_tier_value(self):
        store = GateConfigStore()
        # tier 99 should fall back to T3 value
        assert store.get("min_premium", 99) == store.get("min_premium", _SAFE_DEFAULT_TIER)

    def test_unknown_tier_zero_returns_safe_default(self):
        store = GateConfigStore()
        assert store.get("debounce_ms", 0) == store.get("debounce_ms", _SAFE_DEFAULT_TIER)

    def test_require_oi_all_tiers_default_false(self):
        store = GateConfigStore()
        for tier in _VALID_TIERS:
            assert store.get("require_oi", tier) is False

    def test_dte_multiplier_tier_ordering(self):
        store = GateConfigStore()
        # T1 > T2 > T3
        assert store.get("dte_floor_multiplier", 1) > store.get("dte_floor_multiplier", 2)
        assert store.get("dte_floor_multiplier", 2) > store.get("dte_floor_multiplier", 3)

    def test_debounce_ms_tier_ordering(self):
        store = GateConfigStore()
        # T1 < T2 < T3
        assert store.get("debounce_ms", 1) < store.get("debounce_ms", 2)
        assert store.get("debounce_ms", 2) < store.get("debounce_ms", 3)


# ---------------------------------------------------------------------------
# get_all() — admin snapshot
# ---------------------------------------------------------------------------

class TestGetAll:
    def test_get_all_returns_list(self):
        store = GateConfigStore()
        rows = store.get_all()
        assert isinstance(rows, list)
        assert len(rows) >= len(_FALLBACK)

    def test_get_all_includes_bounds(self):
        store = GateConfigStore()
        for row in store.get_all():
            gate = row["gate_name"]
            if gate in _BOUNDS:
                assert row["min_bound"] is not None
                assert row["max_bound"] is not None

    def test_get_all_all_gates_represented(self):
        store = GateConfigStore()
        rows = store.get_all()
        gates_in_output = {r["gate_name"] for r in rows}
        assert gates_in_output == set(_BOUNDS.keys())


# ---------------------------------------------------------------------------
# TGC-3: Bounds validation — invalid values rejected with ValueError
# ---------------------------------------------------------------------------

class TestBoundsValidation:
    @pytest.mark.asyncio
    async def test_min_premium_zero_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update("min_premium", 3, 0)

    @pytest.mark.asyncio
    async def test_min_premium_below_1000_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update("min_premium", 1, 500)

    @pytest.mark.asyncio
    async def test_min_premium_above_500k_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update("min_premium", 1, 600_000)

    @pytest.mark.asyncio
    async def test_dte_multiplier_below_01_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update("dte_floor_multiplier", 2, 0.05)

    @pytest.mark.asyncio
    async def test_dte_multiplier_above_5_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update("dte_floor_multiplier", 1, 6.0)

    @pytest.mark.asyncio
    async def test_debounce_ms_below_1000_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update("debounce_ms", 1, 100)

    @pytest.mark.asyncio
    async def test_debounce_ms_above_600000_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="outside allowed bounds"):
            await store.update("debounce_ms", 3, 700_000)

    @pytest.mark.asyncio
    async def test_unknown_gate_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="Unknown gate"):
            await store.update("fake_gate", 1, 100)

    @pytest.mark.asyncio
    async def test_invalid_tier_raises(self):
        store = make_store()
        with pytest.raises(ValueError, match="Invalid tier"):
            await store.update("min_premium", 99, 10_000)


# ---------------------------------------------------------------------------
# TGC-4: Market-hours guard
# ---------------------------------------------------------------------------

class TestMarketHoursGuard:
    @pytest.mark.asyncio
    async def test_market_hours_no_confirm_raises(self):
        store = make_store()
        # Patch datetime to a Wednesday 14:00 UTC (market hours)
        market_open_dt = datetime.datetime(
            2026, 5, 6, 14, 0, 0, tzinfo=datetime.timezone.utc
        )
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = market_open_dt
            mock_dt.time = datetime.time
            with pytest.raises(ValueError, match="Market is currently open"):
                await store.update("min_premium", 3, 12_000, confirm_market_hours=False)

    @pytest.mark.asyncio
    async def test_market_hours_with_confirm_proceeds(self):
        store = make_store()
        market_open_dt = datetime.datetime(
            2026, 5, 6, 14, 0, 0, tzinfo=datetime.timezone.utc
        )
        client_mock = mock_http_patch_audit()
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = market_open_dt
            mock_dt.time = datetime.time
            with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                result = await store.update(
                    "min_premium", 3, 12_000, confirm_market_hours=True
                )
        assert result["new_value"] == 12_000

    @pytest.mark.asyncio
    async def test_outside_market_hours_no_confirm_needed(self):
        store = make_store()
        # Saturday 10:00 UTC — outside market hours
        off_hours_dt = datetime.datetime(
            2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        client_mock = mock_http_patch_audit()
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = off_hours_dt
            mock_dt.time = datetime.time
            with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                result = await store.update(
                    "min_premium", 2, 18_000, confirm_market_hours=False
                )
        assert result["new_value"] == 18_000


# ---------------------------------------------------------------------------
# TGC-1/TGC-2: update() — hot-reload behavior
# ---------------------------------------------------------------------------

class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_changes_in_memory_value(self):
        store = make_store()
        off_hours_dt = datetime.datetime(2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)
        client_mock = mock_http_patch_audit()
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = off_hours_dt
            mock_dt.time = datetime.time
            with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                await store.update("min_premium", 3, 12_000)
        assert store.get("min_premium", 3) == 12_000

    @pytest.mark.asyncio
    async def test_update_returns_old_and_new_values(self):
        store = make_store()
        off_hours_dt = datetime.datetime(2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)
        client_mock = mock_http_patch_audit()
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = off_hours_dt
            mock_dt.time = datetime.time
            with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                result = await store.update("min_premium", 3, 12_000)
        assert result["old_value"] == 10_000
        assert result["new_value"] == 12_000
        assert result["gate_name"] == "min_premium"
        assert result["tier"] == 3

    @pytest.mark.asyncio
    async def test_update_increments_epoch(self):
        store = make_store()
        off_hours_dt = datetime.datetime(2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)
        epoch_before = store.epoch
        client_mock = mock_http_patch_audit()
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = off_hours_dt
            mock_dt.time = datetime.time
            with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                await store.update("debounce_ms", 1, 45_000)
        assert store.epoch == epoch_before + 1

    @pytest.mark.asyncio
    async def test_update_db_patch_failure_raises_runtime_error(self):
        store = make_store()
        off_hours_dt = datetime.datetime(2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)
        client_mock = mock_http_patch_audit(patch_status=500)
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = off_hours_dt
            mock_dt.time = datetime.time
            with patch("backend.services.gate_config_store.httpx.AsyncClient", return_value=client_mock):
                with pytest.raises(RuntimeError, match="DB PATCH failed"):
                    await store.update("min_premium", 1, 30_000)

    @pytest.mark.asyncio
    async def test_update_no_db_updates_in_memory_only(self):
        store = GateConfigStore()
        store._supabase_url = None
        store._supabase_key = None
        off_hours_dt = datetime.datetime(2026, 5, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)
        with patch("backend.services.gate_config_store.datetime.datetime") as mock_dt:
            mock_dt.now.return_value = off_hours_dt
            mock_dt.time = datetime.time
            result = await store.update("min_premium", 2, 20_000)
        assert result["new_value"] == 20_000
        assert store.get("min_premium", 2) == 20_000
        assert "_note" in result


# ---------------------------------------------------------------------------
# Type casting
# ---------------------------------------------------------------------------

class TestCasting:
    def test_min_premium_cast_to_int(self):
        store = GateConfigStore()
        casted = store._cast("min_premium", "15000.0")
        assert casted == 15_000
        assert isinstance(casted, int)

    def test_dte_multiplier_cast_to_float(self):
        store = GateConfigStore()
        casted = store._cast("dte_floor_multiplier", "1.5")
        assert casted == 1.5
        assert isinstance(casted, float)

    def test_require_oi_cast_to_bool(self):
        store = GateConfigStore()
        assert store._cast("require_oi", "1") is True
        assert store._cast("require_oi", "0") is False

    def test_debounce_ms_cast_to_int(self):
        store = GateConfigStore()
        assert store._cast("debounce_ms", "30000") == 30_000


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestModuleSingleton:
    def test_module_singleton_exists(self):
        assert gate_config_store is not None
        assert isinstance(gate_config_store, GateConfigStore)

    def test_singleton_has_fallback_values(self):
        for (gate, tier), expected in _FALLBACK.items():
            assert gate_config_store.get(gate, tier) == expected
