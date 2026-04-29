"""
test_tier_engine.py — Unit and integration tests for Feature 4A: Dynamic Tiering

Covers:
  TE-01 … TE-08   tier_engine.assign_tiers() classification logic
  TR-01 … TR-06   symbol_registry per-tier params (_build_tier_params, ContractMeta.tier)
  US-01 … US-03   universe_store.load_tier_map() (mocked Supabase)
  ADM-01 … ADM-06 Admin endpoints: PATCH/GET /tier-thresholds, GET /tier-distribution

All tests are pure-Python / asyncio — no live Supabase, no Tradier, no network.
Updated 2026-04-27: wire .range() into US mock chain for _paginate_symbols compat.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


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

    def test_t1_high_volume_high_price(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("SPY", last_price=500.0, volume=80_000_000, average_volume=80_000_000, open_interest=50_000)
        assert _classify(q, thresh, require_oi=True) == 1

    def test_t2_mid_volume(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("HOOD", last_price=15.0, volume=5_000_000, average_volume=5_000_000, open_interest=800)
        assert _classify(q, thresh, require_oi=True) == 2

    def test_t3_low_volume(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("XYZZ", last_price=3.0, volume=600_000, average_volume=600_000, open_interest=150)
        assert _classify(q, thresh, require_oi=True) == 3

    def test_price_gate_drops_to_t3(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        q = _SQ("CLOV", last_price=4.0, volume=3_000_000, average_volume=3_000_000, open_interest=300)
        assert _classify(q, thresh, require_oi=True) == 3

    def test_oi_gate_drops_t1_to_t2(self):
        from services.tier_engine import _classify
        thresh = {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t2_min_volume":  2_000_000, "t2_min_last_price": 10.0, "t2_min_oi":  500,
            "t3_min_volume":    500_000, "t3_min_last_price":  1.0, "t3_min_oi":  100,
        }
        # OI=500 is at T2 threshold but below T1 (1000) → should land T2 with require_oi=True
        q = _SQ("BIGVOL", last_price=25.0, volume=30_000_000, average_volume=30_000_000, open_interest=500)
        assert _classify(q, thresh, require_oi=True) == 2

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
                result = await tier_engine.assign_tiers(quotes, require_oi=True)
            assert isinstance(result, dict)
            assert result["SPY"]  == 1
            assert result["HOOD"] == 2
            assert result["XYZZ"] == 3
        asyncio.run(_run())

    def test_assign_tiers_fallback_on_db_error(self):
        async def _run():
            from services import tier_engine
            quotes = [_SQ("SPY", 500.0, 80_000_000, 80_000_000, 50_000)]
            with patch.object(tier_engine, '_fetch_thresholds', AsyncMock(side_effect=Exception("DB down"))):
                result = await tier_engine.assign_tiers(quotes)
            assert result["SPY"] == 3
        asyncio.run(_run())

    def test_invalidate_cache_resets_ttl(self):
        import services.tier_engine as te
        te._thresh_cache_ts = 99999.0
        te.invalidate_cache()
        assert te._thresh_cache_ts == 0.0


# ===========================================================================
# TR — SymbolRegistry per-tier params
# ===========================================================================

class TestSymbolRegistryTierParamsTR:

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

    def test_contract_meta_tier_default(self):
        from services.symbol_registry import ContractMeta
        m = ContractMeta(
            ticker="AAPL", strike=180.0, expiry="2026-01-17",
            contract_type="CALL", dte=30, open_interest=5000,
        )
        assert m.tier == 3

    def test_contract_meta_tier_explicit(self):
        from services.symbol_registry import ContractMeta
        m1 = ContractMeta(
            ticker="SPY", strike=500.0, expiry="2026-01-17",
            contract_type="CALL", dte=30, open_interest=50_000, tier=1,
        )
        assert m1.tier == 1

    def test_set_tier_map_updates_map(self):
        from services.symbol_registry import SymbolRegistry
        reg = SymbolRegistry(watchlist=["SPY", "AAPL"], tier_map={"SPY": 1})
        reg.set_tier_map({"SPY": 1, "AAPL": 2})
        assert reg._tier_map["AAPL"] == 2

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

    def test_load_tier_map_returns_dict(self):
        import services.universe_store as us

        mock_client = MagicMock()

        def _make_chain():
            chain = MagicMock()
            for m in ["select", "eq", "order", "limit", "range"]:
                getattr(chain, m).return_value = chain
            return chain

        snap_chain = _make_chain()
        snap_chain.execute.return_value = MagicMock(data=[{"id": "snap-001"}])

        sym_chain = _make_chain()
        sym_chain.execute.side_effect = [
            # page 1: 3 symbols (< _PAGE_SIZE=1000, loop terminates)
            MagicMock(data=[
                {"symbol": "SPY",  "tier": 1},
                {"symbol": "HOOD", "tier": 2},
                {"symbol": "XYZZ", "tier": 3},
            ]),
        ]

        mock_client.table.side_effect = lambda name: (
            snap_chain if "snapshot" in name else sym_chain
        )

        with patch.object(us, '_client', return_value=mock_client):
            result = us._sync_load_tier_map()

        assert result == {"SPY": 1, "HOOD": 2, "XYZZ": 3}

    def test_load_tier_map_empty_on_no_snapshot(self):
        import services.universe_store as us

        mock_client = MagicMock()
        snap_result = MagicMock()
        snap_result.data = []
        chain = MagicMock()
        for m in ["select", "eq", "order", "limit", "range"]:
            getattr(chain, m).return_value = chain
        chain.execute.return_value = snap_result
        mock_client.table.return_value = chain

        with patch.object(us, '_client', return_value=mock_client):
            result = us._sync_load_tier_map()

        assert result == {}

    def test_load_tier_map_empty_on_exception(self):
        import services.universe_store as us

        with patch.object(us, '_client', side_effect=RuntimeError("no service key")):
            result = us._sync_load_tier_map()

        assert result == {}


# ===========================================================================
# ADM — Admin endpoint contract tests
# ===========================================================================

class TestAdminTierEndpointsADM:

    def test_admin_router_has_patch_tier_thresholds(self):
        import ast
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "tier-thresholds" in text
        assert "patch" in text.lower() or "router.patch" in text.lower() or "@router.patch" in text
        _ = ast

    def test_admin_router_has_get_tier_distribution(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "tier-distribution" in text

    def test_patch_tier_thresholds_calls_invalidate_cache(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "invalidate_cache" in text

    def test_patch_tier_thresholds_has_whitelist(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "_TIER_THRESHOLD_COLUMNS" in text or "ALLOWED_COLUMNS" in text or "whitelist" in text.lower() or "TIER_THRESHOLD_KEYS" in text

    def test_admin_router_has_get_tier_thresholds(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "@router.get" in text and "tier-thresholds" in text
        assert "cache" in text and "age_seconds" in text

    def test_patch_invalidate_cache_is_synchronous_not_deferred(self):
        import pathlib
        import ast
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()

        tree = ast.parse(text)
        patch_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_tier_thresholds":
                patch_fn = node
                break

        assert patch_fn is not None

        fn_source = ast.get_source_segment(text, patch_fn) or ""
        assert "invalidate_cache()" in fn_source
