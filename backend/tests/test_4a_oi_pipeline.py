"""
tests/test_4a_oi_pipeline.py

Feature 4A-OI: avg chain OI pipeline tests.

Covers:
  - symbol_registry: OI roll-up in build(), get_oi_map() public method
  - tier_engine._classify: no OI grace path, all 3 conditions enforced
  - main._stamp_oi: stamps avg chain OI onto SymbolQuote objects
  - Integration: build -> get_oi_map -> stamp -> assign_tiers

All tests are pure unit tests (no network, no DB, no Tradier).
External I/O is patched at the module boundary.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Minimal SymbolQuote stub
# ---------------------------------------------------------------------------
@dataclass
class _Quote:
    symbol:          str
    last_price:      float = 0.0
    volume:          int   = 0
    average_volume:  int   = 0
    open_interest:   int   = 0
    stream_eligible: bool  = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_thresh(
    t1_vol=20_000_000, t1_price=10.0, t1_oi=1_000,
    t2_vol=2_000_000,  t2_price=10.0, t2_oi=500,
    t3_vol=500_000,    t3_price=1.0,  t3_oi=100,
) -> dict:
    return {
        "t1_min_volume":     t1_vol,
        "t1_min_last_price": t1_price,
        "t1_min_oi":         t1_oi,
        "t1_atm_pct":        0.20,
        "t1_max_dte":        90,
        "t2_min_volume":     t2_vol,
        "t2_min_last_price": t2_price,
        "t2_min_oi":         t2_oi,
        "t2_atm_pct":        0.15,
        "t2_max_dte":        60,
        "t3_min_volume":     t3_vol,
        "t3_min_last_price": t3_price,
        "t3_min_oi":         t3_oi,
        "t3_atm_pct":        0.10,
        "t3_max_dte":        30,
    }


# ---------------------------------------------------------------------------
# 1. symbol_registry: get_oi_map returns populated dict after build
# ---------------------------------------------------------------------------

class TestSymbolRegistryOiMap:
    def test_get_oi_map_empty_before_build(self):
        from services.symbol_registry import SymbolRegistry
        reg = SymbolRegistry(watchlist=["AAPL", "TSLA"], tier_map={})
        assert reg.get_oi_map() == {}

    @pytest.mark.asyncio
    async def test_oi_map_populated_after_build(self):
        from services.symbol_registry import SymbolRegistry

        async def _fake_build_ticker(self_inner, ticker, stock_price,
                                     registry, oi_by_ticker, tier_params):
            oi_by_ticker[ticker] = 200
            registry[f"{ticker}250117C00100000"] = MagicMock()

        prices      = {"AAPL": 180.0, "TSLA": 250.0}
        raw_quotes  = {"AAPL": {"volume": 1000, "average_volume": 500},
                       "TSLA": {"volume": 2000, "average_volume": 800}}

        reg = SymbolRegistry(watchlist=["AAPL", "TSLA"], tier_map={})
        with patch.object(SymbolRegistry, "_fetch_stock_prices",
                          AsyncMock(return_value=(prices, raw_quotes))), \
             patch.object(SymbolRegistry, "_build_ticker", _fake_build_ticker):
            await reg.build()

        assert reg.get_oi_map() == {"AAPL": 200, "TSLA": 200}

    @pytest.mark.asyncio
    async def test_oi_zero_for_ticker_with_no_contracts(self):
        from services.symbol_registry import SymbolRegistry

        async def _no_contracts(self_inner, ticker, stock_price,
                                registry, oi_by_ticker, tier_params):
            oi_by_ticker[ticker] = 0

        prices     = {"HOOD": 15.0}
        raw_quotes = {"HOOD": {"volume": 500, "average_volume": 300}}

        reg = SymbolRegistry(watchlist=["HOOD"], tier_map={})
        with patch.object(SymbolRegistry, "_fetch_stock_prices",
                          AsyncMock(return_value=(prices, raw_quotes))), \
             patch.object(SymbolRegistry, "_build_ticker", _no_contracts):
            await reg.build()

        assert reg.get_oi_map()["HOOD"] == 0

    @pytest.mark.asyncio
    async def test_get_oi_map_returns_independent_copy(self):
        from services.symbol_registry import SymbolRegistry

        async def _fake_build(self_inner, ticker, stock_price,
                              registry, oi_by_ticker, tier_params):
            oi_by_ticker[ticker] = 500

        prices     = {"SPY": 500.0}
        raw_quotes = {"SPY": {"volume": 10000, "average_volume": 8000}}

        reg = SymbolRegistry(watchlist=["SPY"], tier_map={})
        with patch.object(SymbolRegistry, "_fetch_stock_prices",
                          AsyncMock(return_value=(prices, raw_quotes))), \
             patch.object(SymbolRegistry, "_build_ticker", _fake_build):
            await reg.build()

        copy1 = reg.get_oi_map()
        copy1["SPY"] = 999_999
        assert reg.get_oi_map()["SPY"] == 500


# ---------------------------------------------------------------------------
# 2. tier_engine._classify: OI grace path removed
# ---------------------------------------------------------------------------

class TestClassifyNoGrace:
    def _classify(self, quote, thresh):
        from services.tier_engine import _classify
        return _classify(quote, thresh)

    def test_t1_all_three_conditions_met(self):
        q = _Quote("AAPL", last_price=150.0, average_volume=25_000_000, open_interest=2_000)
        assert self._classify(q, _make_thresh()) == 1

    def test_t1_fails_if_oi_zero(self):
        q = _Quote("AAPL", last_price=150.0, average_volume=25_000_000, open_interest=0)
        tier = self._classify(q, _make_thresh())
        assert tier == 3

    def test_t1_fails_if_oi_below_threshold(self):
        q = _Quote("AAPL", last_price=150.0, average_volume=25_000_000, open_interest=999)
        assert self._classify(q, _make_thresh()) != 1

    def test_t1_fails_if_vol_below_threshold(self):
        q = _Quote("AAPL", last_price=150.0, average_volume=15_000_000, open_interest=2_000)
        assert self._classify(q, _make_thresh()) != 1

    def test_t1_fails_if_price_below_threshold(self):
        q = _Quote("AAPL", last_price=5.0, average_volume=25_000_000, open_interest=2_000)
        assert self._classify(q, _make_thresh()) != 1

    def test_t2_all_three_conditions_met(self):
        q = _Quote("HOOD", last_price=15.0, average_volume=3_000_000, open_interest=600)
        assert self._classify(q, _make_thresh()) == 2

    def test_t2_fails_if_oi_zero(self):
        q = _Quote("HOOD", last_price=15.0, average_volume=3_000_000, open_interest=0)
        assert self._classify(q, _make_thresh()) == 3

    def test_t2_fails_if_oi_below_threshold(self):
        q = _Quote("HOOD", last_price=15.0, average_volume=3_000_000, open_interest=499)
        assert self._classify(q, _make_thresh()) == 3

    def test_t3_floor_when_oi_present_but_below_t2(self):
        q = _Quote("RIVN", last_price=12.0, average_volume=3_000_000, open_interest=200)
        assert self._classify(q, _make_thresh()) == 3

    def test_t3_for_low_vol_symbol(self):
        q = _Quote("XYZ", last_price=20.0, average_volume=100_000, open_interest=5_000)
        assert self._classify(q, _make_thresh()) == 3


# ---------------------------------------------------------------------------
# 3. main._stamp_oi
# ---------------------------------------------------------------------------

class TestStampOi:
    def _stamp_oi(self, quotes, oi_map):
        from main import _stamp_oi
        return _stamp_oi(quotes, oi_map)

    def test_stamps_correct_oi_from_map(self):
        quotes = [_Quote("AAPL"), _Quote("TSLA"), _Quote("SPY")]
        self._stamp_oi(quotes, {"AAPL": 2000, "TSLA": 800, "SPY": 5000})
        assert quotes[0].open_interest == 2000
        assert quotes[1].open_interest == 800
        assert quotes[2].open_interest == 5000

    def test_missing_ticker_gets_zero(self):
        quotes = [_Quote("UNKNOWN", open_interest=999)]
        self._stamp_oi(quotes, {"AAPL": 2000})
        assert quotes[0].open_interest == 0

    def test_mutates_in_place(self):
        quotes = [_Quote("AAPL", open_interest=0)]
        result = self._stamp_oi(quotes, {"AAPL": 1500})
        assert result is None
        assert quotes[0].open_interest == 1500

    def test_empty_quotes_is_noop(self):
        self._stamp_oi([], {"AAPL": 100})

    def test_empty_oi_map_zeros_all(self):
        quotes = [_Quote("AAPL", open_interest=500), _Quote("TSLA", open_interest=300)]
        self._stamp_oi(quotes, {})
        assert all(q.open_interest == 0 for q in quotes)


# ---------------------------------------------------------------------------
# 4. Integration
# ---------------------------------------------------------------------------

class TestOiDrivenTierIntegration:
    @pytest.mark.asyncio
    async def test_oi_drives_t1_demotion_to_t3(self):
        from services.tier_engine import assign_tiers
        from main import _stamp_oi
        quotes = [_Quote("AAPL", last_price=180.0, average_volume=30_000_000)]
        _stamp_oi(quotes, {"AAPL": 0})
        with patch("services.tier_engine._fetch_thresholds",
                   new=AsyncMock(return_value=_make_thresh())):
            tiers = await assign_tiers(quotes)
        assert tiers["AAPL"] == 3

    @pytest.mark.asyncio
    async def test_oi_drives_correct_t1_promotion(self):
        from services.tier_engine import assign_tiers
        from main import _stamp_oi
        quotes = [_Quote("AAPL", last_price=180.0, average_volume=30_000_000)]
        _stamp_oi(quotes, {"AAPL": 2000})
        with patch("services.tier_engine._fetch_thresholds",
                   new=AsyncMock(return_value=_make_thresh())):
            tiers = await assign_tiers(quotes)
        assert tiers["AAPL"] == 1

    @pytest.mark.asyncio
    async def test_mixed_oi_produces_mixed_tiers(self):
        from services.tier_engine import assign_tiers
        from main import _stamp_oi
        quotes = [
            _Quote("SPY",  last_price=500.0, average_volume=50_000_000),
            _Quote("HOOD", last_price=15.0,  average_volume=3_000_000),
            _Quote("SPCE", last_price=2.0,   average_volume=200_000),
        ]
        _stamp_oi(quotes, {"SPY": 5_000, "HOOD": 600, "SPCE": 50})
        with patch("services.tier_engine._fetch_thresholds",
                   new=AsyncMock(return_value=_make_thresh())):
            tiers = await assign_tiers(quotes)
        assert tiers["SPY"] == 1
        assert tiers["HOOD"] == 2
        assert tiers["SPCE"] == 3

    @pytest.mark.asyncio
    async def test_preliminary_vs_final_tier_diff(self):
        from services.tier_engine import assign_tiers
        from main import _stamp_oi
        quotes = [_Quote("NVDA", last_price=900.0, average_volume=25_000_000)]
        with patch("services.tier_engine._fetch_thresholds",
                   new=AsyncMock(return_value=_make_thresh())):
            prelim_tiers = await assign_tiers(quotes)
        _stamp_oi(quotes, {"NVDA": 3_000})
        with patch("services.tier_engine._fetch_thresholds",
                   new=AsyncMock(return_value=_make_thresh())):
            final_tiers = await assign_tiers(quotes)
        assert prelim_tiers["NVDA"] == 3
        assert final_tiers["NVDA"]  == 1
