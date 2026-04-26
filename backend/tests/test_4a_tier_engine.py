"""
test_4a_tier_engine.py — Unit tests for Feature 4A: Dynamic Tiering

Covers:
  TE-01 … TE-08  assign_tiers() logic (tier_engine.py)
  TE-09 … TE-12  _fetch_thresholds() cache behaviour
  TE-13 … TE-16  Admin endpoint column whitelist / PATCH guard
  TE-17 … TE-20  _TierParams dataclass + ContractMeta.tier field (symbol_registry.py)
  TE-21 … TE-22  tier_map round-trip: upsert_symbol_quotes stores tier column
  TE-23 … TE-26  OI grace-path removal regression (Feature 4A-OI)

All tests are pure-Python / asyncio — no Supabase, no network.
"""
import asyncio
from unittest.mock import MagicMock, patch


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# TE-01 … TE-08  assign_tiers()
# ===========================================================================

class TestAssignTiers:
    def _thresholds(self):
        return {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0,
            "t1_min_oi": 1000, "t1_atm_pct": 0.20, "t1_max_dte": 90,
            "t2_min_volume": 2_000_000,  "t2_min_last_price": 10.0,
            "t2_min_oi": 500,  "t2_atm_pct": 0.15, "t2_max_dte": 60,
            "t3_min_volume": 500_000,    "t3_min_last_price": 1.0,
            "t3_min_oi": 100,  "t3_atm_pct": 0.10, "t3_max_dte": 30,
        }

    def _quote(self, volume, price, oi):
        return {"average_volume": volume, "last": price, "open_interest": oi}

    def test_spy_tier1(self):
        from services.tier_engine import assign_tiers
        assert assign_tiers({"SPY": self._quote(80_000_000, 502.0, 50000)}, self._thresholds())["SPY"] == 1

    def test_hood_tier2(self):
        from services.tier_engine import assign_tiers
        assert assign_tiers({"HOOD": self._quote(5_000_000, 25.0, 800)}, self._thresholds())["HOOD"] == 2

    def test_low_volume_tier3(self):
        from services.tier_engine import assign_tiers
        assert assign_tiers({"XYZ": self._quote(600_000, 5.0, 150)}, self._thresholds())["XYZ"] == 3

    def test_below_t3_minimum_still_tier3(self):
        from services.tier_engine import assign_tiers
        assert assign_tiers({"PENNY": self._quote(100_000, 0.50, 10)}, self._thresholds())["PENNY"] == 3

    def test_empty_quotes_returns_empty_dict(self):
        from services.tier_engine import assign_tiers
        assert assign_tiers({}, self._thresholds()) == {}

    def test_multiple_symbols_classified_independently(self):
        from services.tier_engine import assign_tiers
        quotes = {
            "SPY":  self._quote(80_000_000, 502.0, 50000),
            "HOOD": self._quote(5_000_000,  25.0,  800),
            "XYZ":  self._quote(600_000,    5.0,   150),
        }
        result = assign_tiers(quotes, self._thresholds())
        assert result["SPY"] == 1 and result["HOOD"] == 2 and result["XYZ"] == 3

    def test_tier1_boundary_exactly_at_threshold(self):
        from services.tier_engine import assign_tiers
        assert assign_tiers({"EDGE": self._quote(20_000_000, 10.0, 1000)}, self._thresholds())["EDGE"] == 1

    def test_missing_fields_default_to_zero_not_crash(self):
        from services.tier_engine import assign_tiers
        assert assign_tiers({"BARE": {}}, self._thresholds())["BARE"] == 3


# ===========================================================================
# TE-09 … TE-12  _fetch_thresholds() cache
# ===========================================================================

class TestFetchThresholds:
    def test_fetch_thresholds_returns_dict(self):
        from services import tier_engine
        mock_row = {
            "t1_min_volume": 20000000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t1_atm_pct": 0.20, "t1_max_dte": 90,
            "t2_min_volume": 2000000,  "t2_min_last_price": 10.0, "t2_min_oi": 500,
            "t2_atm_pct": 0.15, "t2_max_dte": 60,
            "t3_min_volume": 500000,   "t3_min_last_price": 1.0,  "t3_min_oi": 100,
            "t3_atm_pct": 0.10, "t3_max_dte": 30,
        }
        sb = MagicMock()
        q  = MagicMock()
        q.select.return_value = q
        q.eq.return_value     = q
        q.limit.return_value  = q
        q.execute.return_value = MagicMock(data=[mock_row])
        sb.table.return_value = q
        with patch.object(tier_engine, "_client", return_value=sb):
            tier_engine._thresholds_cache = None
            result = tier_engine._fetch_thresholds()
        assert isinstance(result, dict)
        assert result["t1_min_volume"] == 20000000

    def test_fetch_thresholds_uses_cache_on_second_call(self):
        from services import tier_engine
        cached = {"t1_min_volume": 99999999}
        tier_engine._thresholds_cache    = cached
        tier_engine._thresholds_cache_ts = 9_999_999_999
        assert tier_engine._fetch_thresholds() is cached

    def test_fetch_thresholds_falls_back_to_defaults_on_error(self):
        from services import tier_engine
        with patch.object(tier_engine, "_client", side_effect=Exception("DB down")):
            tier_engine._thresholds_cache = None
            result = tier_engine._fetch_thresholds()
        assert isinstance(result, dict) and "t1_min_volume" in result

    def test_invalidate_clears_cache(self):
        from services import tier_engine
        tier_engine._thresholds_cache = {"t1_min_volume": 1}
        tier_engine.invalidate_thresholds_cache()
        assert tier_engine._thresholds_cache is None


# ===========================================================================
# TE-13 … TE-16  Admin endpoint
# ===========================================================================

class TestAdminTierThresholds:
    def _admin_src(self):
        import pathlib
        p = pathlib.Path("backend/routers/admin.py")
        if not p.exists():
            p = pathlib.Path("routers/admin.py")
        return p.read_text()

    def test_admin_module_has_patch_tier_thresholds_route(self):
        text = self._admin_src()
        assert "tier-thresholds" in text or "tier_thresholds" in text

    def test_admin_tier_thresholds_route_has_whitelist(self):
        text = self._admin_src()
        assert "ALLOWED_TIER_COLUMNS" in text or "allowed_columns" in text.lower()

    def test_admin_tier_distribution_route_exists(self):
        text = self._admin_src()
        assert "tier-distribution" in text or "tier_distribution" in text

    def test_admin_invalidates_cache_after_patch(self):
        assert "invalidate_thresholds_cache" in self._admin_src()


# ===========================================================================
# TE-17 … TE-20  _TierParams + ContractMeta.tier
# ===========================================================================

class TestTierParamsAndContractMeta:
    def test_tier_params_dataclass_exists(self):
        from services.symbol_registry import _TierParams
        assert hasattr(_TierParams, "__dataclass_fields__") or hasattr(_TierParams, "atm_pct")

    def test_tier_params_has_atm_pct_and_max_dte(self):
        from services.symbol_registry import _TierParams
        p = _TierParams(atm_pct=0.20, max_dte=90)
        assert p.atm_pct == 0.20 and p.max_dte == 90

    def test_contract_meta_has_tier_field(self):
        import inspect
        from services.symbol_registry import ContractMeta
        if hasattr(ContractMeta, "__dataclass_fields__"):
            fields = set(ContractMeta.__dataclass_fields__.keys())
        else:
            fields = set(inspect.signature(ContractMeta.__init__).parameters.keys()) - {"self"}
        assert "tier" in fields

    def test_tier_params_t1_wider_than_t3(self):
        from services.symbol_registry import _TierParams
        t1 = _TierParams(atm_pct=0.20, max_dte=90)
        t3 = _TierParams(atm_pct=0.10, max_dte=30)
        assert t1.atm_pct > t3.atm_pct and t1.max_dte > t3.max_dte


# ===========================================================================
# TE-21 … TE-22  tier_map round-trip
# ===========================================================================

class TestTierMapRoundTrip:
    def test_upsert_symbol_quotes_accepts_tier_map(self):
        import inspect
        from services.universe_store import upsert_symbol_quotes
        assert "tier_map" in inspect.signature(upsert_symbol_quotes).parameters

    def test_upsert_symbol_quotes_writes_tier_to_row(self):
        from services import universe_store
        sb = MagicMock()
        q  = MagicMock()
        q.upsert.return_value  = q
        q.execute.return_value = MagicMock(data=[])
        sb.table.return_value  = q
        quotes   = {"SPY": {"last": 502.0, "open_interest": 50000, "average_volume": 80_000_000}}
        tier_map = {"SPY": 1}
        with patch.object(universe_store, "_client", return_value=sb):
            universe_store._sync_upsert_symbol_quotes(quotes, tier_map=tier_map)
        rows = q.upsert.call_args_list[0].args[0]
        sprow = (rows if not isinstance(rows, list) else next(r for r in rows if r.get("symbol") == "SPY"))
        assert sprow.get("tier") == 1


# ===========================================================================
# TE-23 … TE-26  OI grace-path removal regression
# ===========================================================================

class TestOiGracePathRemoved:
    def _thresh(self):
        return {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0, "t1_min_oi": 1_000,
            "t1_atm_pct": 0.20, "t1_max_dte": 90,
            "t2_min_volume": 2_000_000,  "t2_min_last_price": 10.0, "t2_min_oi": 500,
            "t2_atm_pct": 0.15, "t2_max_dte": 60,
            "t3_min_volume": 500_000,    "t3_min_last_price": 1.0,  "t3_min_oi": 100,
            "t3_atm_pct": 0.10, "t3_max_dte": 30,
        }

    def _q(self, sym, vol, price, oi):
        q = MagicMock()
        q.symbol = sym; q.average_volume = vol; q.volume = vol
        q.last_price = price; q.open_interest = oi
        return q

    def test_oi_zero_t1_vol_price_yields_t3_not_t1(self):
        from services.tier_engine import _classify
        assert _classify(self._q("AAPL", 25_000_000, 150.0, 0), self._thresh()) == 3

    def test_oi_zero_t2_vol_price_yields_t3_not_t2(self):
        from services.tier_engine import _classify
        assert _classify(self._q("HOOD", 3_000_000, 15.0, 0), self._thresh()) == 3

    def test_real_oi_at_t1_threshold_promotes_to_t1(self):
        from services.tier_engine import _classify
        assert _classify(self._q("NVDA", 25_000_000, 900.0, 1_000), self._thresh()) == 1

    def test_real_oi_one_below_t1_threshold_stays_t2(self):
        from services.tier_engine import _classify
        assert _classify(self._q("NVDA", 25_000_000, 900.0, 999), self._thresh()) == 2
