"""
test_tier_engine.py — Unit and integration tests for Feature 4A: Dynamic Tiering

Covers:
  TE-01 … TE-08   tier_engine.assign_tiers() classification logic
  TR-01 … TR-06   symbol_registry per-tier params (_build_tier_params, ContractMeta.tier)
  US-01 … US-03   universe_store.load_tier_map() (mocked Supabase)
  ADM-01 … ADM-04 Admin endpoints: PATCH /tier-thresholds, GET /tier-distribution

All tests are pure-Python / asyncio — no live Supabase, no Tradier, no network.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from typing import Optional


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Minimal SymbolQuote stub (mirrors services.symbols_loader.SymbolQuote)
# ---------------------------------------------------------------------------

@dataclass
class _SQ:
    symbol:         str
    last_price:     float
    volume:         int
    average_volume: int
    open_interest:  int
    stream_eligible: bool = True


# ===========================================================================
# TE — tier_engine.assign_tiers()
# ===========================================================================

class TestTierEngineTE:
    """Feature 4A: assign_tiers() produces correct T1/T2/T3 classifications."""

    # TE-01: T1 symbol must have volume >= t1_min_volume AND price >= t1_min_last_price
    def test_t1_high_volume_high_price(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("SPY", last_price=500.0, volume=80_000_000, average_volume=80_000_000, open_interest=50_000)
        assert _classify(q, thresh) == 1

    # TE-02: Symbol just below T1 volume threshold falls into T2
    def test_t2_mid_volume(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("HOOD", last_price=15.0, volume=5_000_000, average_volume=5_000_000, open_interest=800)
        assert _classify(q, thresh) == 2

    # TE-03: Low volume / low OI → T3
    def test_t3_low_volume(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("XYZZ", last_price=3.0, volume=600_000, average_volume=600_000, open_interest=150)
        assert _classify(q, thresh) == 3

    # TE-04: Price below t2_min_last_price bumps down to T3 even with T2 volume
    def test_price_gate_drops_to_t3(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        # Volume qualifies for T2 but price < $10 → T3
        q = _SQ("CLOV", last_price=4.0, volume=3_000_000, average_volume=3_000_000, open_interest=300)
        assert _classify(q, thresh) == 3

    # TE-05: OI gate — T1 volume but OI too low → T2
    def test_oi_gate_drops_t1_to_t2(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("BIGVOL", last_price=25.0, volume=30_000_000, average_volume=30_000_000, open_interest=500)  # OI < 1000
        assert _classify(q, thresh) == 2

    # TE-06: assign_tiers() returns dict[str, int]
    def test_assign_tiers_returns_dict(self):
        async def _run():
            from services import tier_engine
            dummy_thresh = {
                "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
                "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
                "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
            }
            quotes = [
                _SQ("SPY", 500.0, 80_000_000, 80_000_000, 50_000),
                _SQ("HOOD", 15.0, 5_000_000,  5_000_000,    800),
                _SQ("XYZZ",  3.0,   600_000,    600_000,    150),
            ]
            with patch.object(tier_engine, '_fetch_thresholds', AsyncMock(return_value=dummy_thresh)):
                result = await tier_engine.assign_tiers(quotes)
            assert isinstance(result, dict)
            assert result["SPY"]  == 1
            assert result["HOOD"] == 2
            assert result["XYZZ"] == 3
        run(_run())

    # TE-07: assign_tiers() falls back to T3 for all symbols on DB error
    def test_assign_tiers_fallback_on_db_error(self):
        async def _run():
            from services import tier_engine
            quotes = [
                _SQ("SPY", 500.0, 80_000_000, 80_000_000, 50_000),
            ]
            with patch.object(tier_engine, '_fetch_thresholds', AsyncMock(side_effect=Exception("DB down"))):
                result = await tier_engine.assign_tiers(quotes)
            assert result["SPY"] == 3, "Must fall back to T3 on threshold fetch failure"
        run(_run())

    # TE-08: invalidate_cache() resets the TTL so next call re-fetches from DB
    def test_invalidate_cache_resets_ttl(self):
        import services.tier_engine as te
        te._thresh_cache_ts = 99999.0   # pretend cache is warm
        te.invalidate_cache()
        assert te._thresh_cache_ts == 0.0


# ===========================================================================
# TR — SymbolRegistry per-tier params
# ===========================================================================

class TestSymbolRegistryTierParamsTR:
    """Feature 4A: _build_tier_params + ContractMeta.tier."""

    # TR-01: _build_tier_params produces correct ATM% and DTE per tier
    def test_build_tier_params_values(self):
        from services.symbol_registry import _build_tier_params
        thresh = {
            "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi": 500,
            "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi": 200,
            "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi":  50,
        }
        params = _build_tier_params(thresh, global_min_oi=0)
        assert params[1].atm_pct == 0.20
        assert params[1].max_dte == 90
        assert params[2].atm_pct == 0.15
        assert params[2].max_dte == 60
        assert params[3].atm_pct == 0.10
        assert params[3].max_dte == 30

    # TR-02: global_min_oi acts as floor — tier min_oi cannot go below it
    def test_global_min_oi_floor(self):
        from services.symbol_registry import _build_tier_params
        thresh = {
            "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi":   0,
            "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi":   0,
            "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi":   0,
        }
        params = _build_tier_params(thresh, global_min_oi=100)
        assert params[1].min_oi == 100
        assert params[2].min_oi == 100
        assert params[3].min_oi == 100

    # TR-03: ContractMeta.tier defaults to 3
    def test_contract_meta_tier_default(self):
        from services.symbol_registry import ContractMeta
        m = ContractMeta(
            ticker="AAPL", strike=180.0, expiry="2026-01-17",
            contract_type="CALL", dte=30, open_interest=5000,
        )
        assert m.tier == 3

    # TR-04: ContractMeta.tier can be set to 1 or 2
    def test_contract_meta_tier_explicit(self):
        from services.symbol_registry import ContractMeta
        m1 = ContractMeta(
            ticker="SPY", strike=500.0, expiry="2026-01-17",
            contract_type="CALL", dte=30, open_interest=50_000, tier=1,
        )
        assert m1.tier == 1

    # TR-05: SymbolRegistry.set_tier_map() updates internal tier_map
    def test_set_tier_map_updates_map(self):
        from services.symbol_registry import SymbolRegistry
        reg = SymbolRegistry(watchlist=["SPY", "AAPL"], tier_map={"SPY": 1})
        reg.set_tier_map({"SPY": 1, "AAPL": 2})
        assert reg._tier_map["AAPL"] == 2

    # TR-06: init_registry propagates tier_map to the singleton
    def test_init_registry_tier_map(self):
        from services.symbol_registry import init_registry, get_registry
        tier_map = {"SPY": 1, "HOOD": 2}
        reg = init_registry(watchlist=["SPY", "HOOD"], tier_map=tier_map)
        assert reg._tier_map["SPY"] == 1
        assert reg._tier_map["HOOD"] == 2
        assert get_registry() is reg


# ===========================================================================
# US — universe_store.load_tier_map() (mocked)
# ===========================================================================

class TestUniverseStoreTierMapUS:
    """Feature 4A: load_tier_map() returns dict[symbol -> tier] from active snapshot."""

    # US-01: Returns correct mapping when DB has a valid active snapshot
    def test_load_tier_map_returns_dict(self):
        import services.universe_store as us

        mock_client = MagicMock()
        snap_result = MagicMock()
        snap_result.data = [{"id": "snap-001"}]
        sym_result  = MagicMock()
        sym_result.data = [
            {"symbol": "SPY",  "tier": 1},
            {"symbol": "HOOD", "tier": 2},
            {"symbol": "XYZZ", "tier": 3},
        ]

        # Chain mock for .table().select().eq().order().limit().execute()
        def make_chain(result):
            chain = MagicMock()
            chain.select.return_value  = chain
            chain.eq.return_value      = chain
            chain.order.return_value   = chain
            chain.limit.return_value   = chain
            chain.execute.return_value = result
            return chain

        mock_client.table.side_effect = lambda name: (
            make_chain(snap_result) if "snapshot" in name else make_chain(sym_result)
        )

        with patch.object(us, '_client', return_value=mock_client):
            result = us._sync_load_tier_map()

        assert result == {"SPY": 1, "HOOD": 2, "XYZZ": 3}

    # US-02: Returns {} when no active snapshot exists
    def test_load_tier_map_empty_on_no_snapshot(self):
        import services.universe_store as us

        mock_client = MagicMock()
        snap_result = MagicMock()
        snap_result.data = []   # no active snapshot
        chain = MagicMock()
        chain.select.return_value  = chain
        chain.eq.return_value      = chain
        chain.order.return_value   = chain
        chain.limit.return_value   = chain
        chain.execute.return_value = snap_result
        mock_client.table.return_value = chain

        with patch.object(us, '_client', return_value=mock_client):
            result = us._sync_load_tier_map()

        assert result == {}

    # US-03: Returns {} (non-fatal) on DB exception
    def test_load_tier_map_empty_on_exception(self):
        import services.universe_store as us

        with patch.object(us, '_client', side_effect=RuntimeError("no service key")):
            result = us._sync_load_tier_map()

        assert result == {}


# ===========================================================================
# ADM — Admin endpoint contract tests (no HTTP server, import-level)
# ===========================================================================

class TestAdminTierEndpointsADM:
    """Feature 4A: admin router exposes PATCH /tier-thresholds + GET /tier-distribution."""

    # ADM-01: Admin router must export tier-thresholds PATCH route
    def test_admin_router_has_patch_tier_thresholds(self):
        import ast, pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "tier-thresholds" in text, \
            "admin.py must contain a /tier-thresholds endpoint (PATCH)"
        assert "patch" in text.lower() or "router.patch" in text.lower() or "@router.patch" in text, \
            "admin.py must have @router.patch for tier-thresholds"

    # ADM-02: Admin router must export tier-distribution GET route
    def test_admin_router_has_get_tier_distribution(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "tier-distribution" in text, \
            "admin.py must contain a /tier-distribution endpoint (GET)"

    # ADM-03: PATCH tier-thresholds must call tier_engine.invalidate_cache()
    def test_patch_tier_thresholds_calls_invalidate_cache(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "invalidate_cache" in text, (
            "PATCH /tier-thresholds must call tier_engine.invalidate_cache() "
            "so the in-process 5-min cache is busted immediately."
        )

    # ADM-04: Column whitelist present (injection prevention)
    def test_patch_tier_thresholds_has_whitelist(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "_TIER_THRESHOLD_COLUMNS" in text or "ALLOWED_COLUMNS" in text or "whitelist" in text.lower() or "TIER_THRESHOLD_KEYS" in text, (
            "PATCH /tier-thresholds must use a column whitelist to reject unknown keys."
        )
