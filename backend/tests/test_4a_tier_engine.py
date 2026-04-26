"""
test_4a_tier_engine.py — Unit tests for Feature 4A: Dynamic Tiering

Covers:
  TE-01 … TE-08  assign_tiers() logic (tier_engine.py) — async, list[SymbolQuote]
  TE-09 … TE-12  _fetch_thresholds() cache behaviour
  TE-13 … TE-16  Admin endpoint column whitelist / PATCH guard
  TE-17 … TE-20  _TierParams dataclass + ContractMeta.tier field (symbol_registry.py)
  TE-21 … TE-22  tier_map round-trip: upsert_symbol_quotes stores tier column
  TE-23 … TE-26  OI grace-path removal regression (Feature 4A-OI)

All tests are pure-Python / asyncio — no Supabase, no network.
"""
import asyncio
import time
from unittest.mock import MagicMock, patch
from dataclasses import dataclass


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Minimal SymbolQuote stub matching the interface used by _classify
# ---------------------------------------------------------------------------
@dataclass
class _SQ:
    symbol:          str
    last_price:      float = 0.0
    volume:          int   = 0
    average_volume:  int   = 0
    open_interest:   int   = 0
    stream_eligible: bool  = True


def _thresh(
    t1_vol=20_000_000, t1_price=10.0, t1_oi=1_000,
    t2_vol=2_000_000,  t2_price=10.0, t2_oi=500,
    t3_vol=500_000,    t3_price=1.0,  t3_oi=100,
) -> dict:
    return {
        "t1_min_volume":     t1_vol,  "t1_min_last_price": t1_price, "t1_min_oi": t1_oi,
        "t1_atm_pct":        0.20,    "t1_max_dte":        90,
        "t2_min_volume":     t2_vol,  "t2_min_last_price": t2_price, "t2_min_oi": t2_oi,
        "t2_atm_pct":        0.15,    "t2_max_dte":        60,
        "t3_min_volume":     t3_vol,  "t3_min_last_price": t3_price, "t3_min_oi": t3_oi,
        "t3_atm_pct":        0.10,    "t3_max_dte":        30,
    }


# ===========================================================================
# TE-01 … TE-08  assign_tiers()  [async, takes list[SymbolQuote]]
# ===========================================================================

class TestAssignTiers:
    def test_spy_tier1(self):
        from services.tier_engine import assign_tiers
        q = _SQ("SPY", last_price=502.0, average_volume=80_000_000, open_interest=50000)
        result = run(assign_tiers([q], thresholds=_thresh()))
        assert result["SPY"] == 1

    def test_hood_tier2(self):
        from services.tier_engine import assign_tiers
        q = _SQ("HOOD", last_price=25.0, average_volume=5_000_000, open_interest=800)
        result = run(assign_tiers([q], thresholds=_thresh()))
        assert result["HOOD"] == 2

    def test_low_volume_tier3(self):
        from services.tier_engine import assign_tiers
        q = _SQ("XYZ", last_price=5.0, average_volume=600_000, open_interest=150)
        assert run(assign_tiers([q], thresholds=_thresh()))["XYZ"] == 3

    def test_below_t3_minimum_still_tier3(self):
        from services.tier_engine import assign_tiers
        q = _SQ("PENNY", last_price=0.5, average_volume=100_000, open_interest=10)
        assert run(assign_tiers([q], thresholds=_thresh()))["PENNY"] == 3

    def test_empty_quotes_returns_empty_dict(self):
        from services.tier_engine import assign_tiers
        assert run(assign_tiers([], thresholds=_thresh())) == {}

    def test_multiple_symbols_classified_independently(self):
        from services.tier_engine import assign_tiers
        quotes = [
            _SQ("SPY",  last_price=502.0, average_volume=80_000_000, open_interest=50000),
            _SQ("HOOD", last_price=25.0,  average_volume=5_000_000,  open_interest=800),
            _SQ("XYZ",  last_price=5.0,   average_volume=600_000,    open_interest=150),
        ]
        result = run(assign_tiers(quotes, thresholds=_thresh()))
        assert result["SPY"] == 1 and result["HOOD"] == 2 and result["XYZ"] == 3

    def test_tier1_boundary_exactly_at_threshold(self):
        from services.tier_engine import assign_tiers
        q = _SQ("EDGE", last_price=10.0, average_volume=20_000_000, open_interest=1000)
        assert run(assign_tiers([q], thresholds=_thresh()))["EDGE"] == 1

    def test_missing_oi_defaults_to_tier3(self):
        from services.tier_engine import assign_tiers
        # open_interest=0 (default) means OI gate fails — falls to T3
        q = _SQ("BARE", last_price=50.0, average_volume=30_000_000, open_interest=0)
        assert run(assign_tiers([q], thresholds=_thresh()))["BARE"] == 3


# ===========================================================================
# TE-09 … TE-12  _fetch_thresholds() cache
# ===========================================================================

class TestFetchThresholds:
    def test_fetch_thresholds_returns_defaults_when_no_supabase(self):
        from services import tier_engine
        # With no SUPABASE_URL/KEY configured _fetch_thresholds returns defaults
        tier_engine.invalidate_cache()
        with patch.object(tier_engine, "_SUPABASE_URL", None), \
             patch.object(tier_engine, "_SUPABASE_KEY", None):
            result = run(tier_engine._fetch_thresholds())
        assert isinstance(result, dict)
        assert result["t1_min_volume"] == 20_000_000

    def test_fetch_thresholds_uses_cache_on_second_call(self):
        from services import tier_engine
        # Prime the cache manually.
        # IMPORTANT: explicit override key must come AFTER **_thresh() so it
        # wins the dict merge — earlier keys are overwritten by later ones.
        tier_engine._cache    = {**_thresh(), "t1_min_volume": 99_999_999}
        tier_engine._cache_ts = time.monotonic() + 9_999
        with patch.object(tier_engine, "_SUPABASE_URL", None), \
             patch.object(tier_engine, "_SUPABASE_KEY", None):
            result = run(tier_engine._fetch_thresholds())
        assert result["t1_min_volume"] == 99_999_999
        # restore
        tier_engine.invalidate_cache()

    def test_fetch_thresholds_falls_back_to_defaults_on_http_error(self):
        from services import tier_engine
        tier_engine.invalidate_cache()
        with patch.object(tier_engine, "_SUPABASE_URL", "http://fake"), \
             patch.object(tier_engine, "_SUPABASE_KEY", "fake"), \
             patch("httpx.AsyncClient.get", side_effect=Exception("network error")):
            # Even on error the function returns the defaults dict
            result = run(tier_engine._fetch_thresholds())
        assert isinstance(result, dict) and "t1_min_volume" in result

    def test_invalidate_clears_cache(self):
        from services import tier_engine
        tier_engine._cache    = {"t1_min_volume": 1}
        tier_engine._cache_ts = 9_999_999.0
        tier_engine.invalidate_thresholds_cache()
        assert tier_engine._cache_ts == 0.0


# ===========================================================================
# TE-13 … TE-16  Admin endpoint source-level checks
# ===========================================================================

class TestAdminTierThresholds:
    def _admin_src(self):
        import pathlib
        for p in [pathlib.Path("backend/routers/admin.py"),
                  pathlib.Path("routers/admin.py")]:
            if p.exists():
                return p.read_text()
        raise FileNotFoundError("admin.py not found")

    def test_admin_module_has_patch_tier_thresholds_route(self):
        text = self._admin_src()
        assert "tier-thresholds" in text or "tier_thresholds" in text

    def test_admin_tier_thresholds_route_has_whitelist(self):
        text = self._admin_src()
        assert "_ALLOWED_TIER_COLUMNS" in text or "allowed_columns" in text.lower()

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
        from services.tier_engine import _TierParams
        assert hasattr(_TierParams, "__dataclass_fields__") or hasattr(_TierParams, "atm_pct")

    def test_tier_params_has_atm_pct_and_max_dte(self):
        from services.tier_engine import _TierParams
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
        from services.tier_engine import _TierParams
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
        tbl = MagicMock()
        # .select().eq().order().limit().execute() returns snapshot_id
        tbl.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = \
            MagicMock(data=[{"id": "snap-123"}])
        # .upsert().execute() returns success
        tbl.upsert.return_value.execute.return_value = MagicMock(data=[])
        sb.table.return_value = tbl

        # _sync_upsert_symbol_quotes expects a list of objects with .symbol, .last_price, etc.
        quote = _SQ(
            symbol="SPY",
            last_price=502.0,
            volume=80_000_000,
            average_volume=80_000_000,
            open_interest=50_000,
            stream_eligible=True,
        )
        tier_map = {"SPY": 1}

        with patch.object(universe_store, "_client", return_value=sb):
            universe_store._sync_upsert_symbol_quotes([quote], tier_map=tier_map)

        # Verify upsert was called and the row carries tier=1
        assert tbl.upsert.called, "expected upsert to be called"
        rows = tbl.upsert.call_args_list[0].args[0]
        spy_row = rows if not isinstance(rows, list) else next(
            r for r in rows if r.get("symbol") == "SPY"
        )
        assert spy_row.get("tier") == 1


# ===========================================================================
# TE-23 … TE-26  OI grace-path removal regression
# ===========================================================================

class TestOiGracePathRemoved:
    def test_oi_zero_t1_vol_price_yields_t3_not_t1(self):
        from services.tier_engine import _classify
        q = _SQ("AAPL", last_price=150.0, average_volume=25_000_000, open_interest=0)
        assert _classify(q, _thresh()) == 3

    def test_oi_zero_t2_vol_price_yields_t3_not_t2(self):
        from services.tier_engine import _classify
        q = _SQ("HOOD", last_price=15.0, average_volume=3_000_000, open_interest=0)
        assert _classify(q, _thresh()) == 3

    def test_real_oi_at_t1_threshold_promotes_to_t1(self):
        from services.tier_engine import _classify
        q = _SQ("NVDA", last_price=900.0, average_volume=25_000_000, open_interest=1_000)
        assert _classify(q, _thresh()) == 1

    def test_real_oi_one_below_t1_threshold_stays_t2(self):
        from services.tier_engine import _classify
        q = _SQ("NVDA", last_price=900.0, average_volume=25_000_000, open_interest=999)
        assert _classify(q, _thresh()) == 2
