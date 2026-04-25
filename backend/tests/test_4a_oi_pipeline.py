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
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Minimal SymbolQuote stub (mirrors services/symbols_loader.SymbolQuote fields
# used by tier_engine and universe_store)
# ---------------------------------------------------------------------------
@dataclass
class _Quote:
    symbol:         str
    last_price:     float    = 0.0
    volume:         int      = 0
    average_volume: int      = 0
    open_interest:  int      = 0
    stream_eligible: bool    = True


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
    """
    Tests for SymbolRegistry._oi_by_ticker and get_oi_map().
    We stub _build_ticker to control which OI values are loaded.
    """

    def test_get_oi_map_empty_before_build(self):
        """get_oi_map() returns {} before build() has run."""
        from services.symbol_registry import SymbolRegistry
        reg = SymbolRegistry(watchlist=["AAPL", "TSLA"], tier_map={})
        result = reg.get_oi_map()
        assert result == {}

    @pytest.mark.asyncio
    async def test_oi_map_populated_after_build(self):
        """
        After build(), get_oi_map() returns avg OI for every ticker
        whose contracts were loaded.
        """
        from services.symbol_registry import SymbolRegistry

        # Patch _build_ticker so it writes synthetic OI into oi_by_ticker
        # and returns a small non-empty registry dict entry.
        async def _fake_build_ticker(self_inner, ticker, registry, oi_by_ticker, tier):
            # Simulate loading 3 contracts with OIs 100, 200, 300 -> avg 200
            oi_by_ticker[ticker] = 200
            registry[f"{ticker}250117C00100000"] = MagicMock()

        reg = SymbolRegistry(watchlist=["AAPL", "TSLA"], tier_map={})

        with patch.object(
            type(reg), "_build_ticker",
            new=_fake_build_ticker,
        ):
            await reg.build()

        oi = reg.get_oi_map()
        assert oi == {"AAPL": 200, "TSLA": 200}

    @pytest.mark.asyncio
    async def test_oi_zero_for_ticker_with_no_contracts(self):
        """
        Tickers whose _build_ticker loads no contracts get oi=0 in the map.
        """
        from services.symbol_registry import SymbolRegistry

        async def _no_contracts(self_inner, ticker, registry, oi_by_ticker, tier):
            # No contracts loaded; oi_by_ticker[ticker] set to 0
            oi_by_ticker[ticker] = 0

        reg = SymbolRegistry(watchlist=["HOOD"], tier_map={})

        with patch.object(type(reg), "_build_ticker", new=_no_contracts):
            await reg.build()

        oi = reg.get_oi_map()
        assert oi["HOOD"] == 0

    @pytest.mark.asyncio
    async def test_get_oi_map_returns_independent_copy(self):
        """
        get_oi_map() returns a copy — mutating it does not affect
        the internal _oi_by_ticker dict.
        """
        from services.symbol_registry import SymbolRegistry

        async def _fake_build(self_inner, ticker, registry, oi_by_ticker, tier):
            oi_by_ticker[ticker] = 500

        reg = SymbolRegistry(watchlist=["SPY"], tier_map={})
        with patch.object(type(reg), "_build_ticker", new=_fake_build):
            await reg.build()

        copy1 = reg.get_oi_map()
        copy1["SPY"] = 999_999          # mutate the copy

        copy2 = reg.get_oi_map()
        assert copy2["SPY"] == 500      # internal state unchanged


# ---------------------------------------------------------------------------
# 2. tier_engine._classify: OI grace path removed
# ---------------------------------------------------------------------------

class TestClassifyNoGrace:
    """
    Verifies that _classify() enforces ALL THREE conditions for T1/T2
    and that oi=0 always results in T3 regardless of vol and price.
    """

    def _classify(self, quote, thresh):
        from services.tier_engine import _classify
        return _classify(quote, thresh)

    # --- T1 tests ---

    def test_t1_all_three_conditions_met(self):
        """T1 when vol, price, AND oi all meet T1 thresholds."""
        q = _Quote("AAPL", last_price=150.0, average_volume=25_000_000, open_interest=2_000)
        assert self._classify(q, _make_thresh()) == 1

    def test_t1_fails_if_oi_zero(self):
        """
        oi=0 must NOT be graced into T1 even if vol+price qualify.
        Old grace path would have returned 1 — this must now return 3.
        """
        q = _Quote("AAPL", last_price=150.0, average_volume=25_000_000, open_interest=0)
        tier = self._classify(q, _make_thresh())
        assert tier == 3, (
            f"Expected T3 (OI grace removed) but got T{tier}. "
            "_classify() must not promote oi=0 symbols to T1."
        )

    def test_t1_fails_if_oi_below_threshold(self):
        """oi=999 just below t1_min_oi=1000 must not get T1."""
        q = _Quote("AAPL", last_price=150.0, average_volume=25_000_000, open_interest=999)
        # May still qualify for T2 depending on T2 thresholds — just not T1
        tier = self._classify(q, _make_thresh())
        assert tier != 1

    def test_t1_fails_if_vol_below_threshold(self):
        """vol below T1 min, even with good price+oi, must not get T1."""
        q = _Quote("AAPL", last_price=150.0, average_volume=15_000_000, open_interest=2_000)
        tier = self._classify(q, _make_thresh())
        assert tier != 1

    def test_t1_fails_if_price_below_threshold(self):
        """price below T1 min, even with good vol+oi, must not get T1."""
        q = _Quote("AAPL", last_price=5.0, average_volume=25_000_000, open_interest=2_000)
        tier = self._classify(q, _make_thresh())
        assert tier != 1

    # --- T2 tests ---

    def test_t2_all_three_conditions_met(self):
        """T2 when vol, price, AND oi all meet T2 (but not T1) thresholds."""
        q = _Quote("HOOD", last_price=15.0, average_volume=3_000_000, open_interest=600)
        assert self._classify(q, _make_thresh()) == 2

    def test_t2_fails_if_oi_zero(self):
        """
        oi=0 must NOT be graced into T2 even if vol+price qualify.
        Old grace path would have returned 2 — this must now return 3.
        """
        q = _Quote("HOOD", last_price=15.0, average_volume=3_000_000, open_interest=0)
        tier = self._classify(q, _make_thresh())
        assert tier == 3, (
            f"Expected T3 (OI grace removed) but got T{tier}. "
            "_classify() must not promote oi=0 symbols to T2."
        )

    def test_t2_fails_if_oi_below_threshold(self):
        """oi=499 just below t2_min_oi=500 must not get T2."""
        q = _Quote("HOOD", last_price=15.0, average_volume=3_000_000, open_interest=499)
        tier = self._classify(q, _make_thresh())
        assert tier == 3

    # --- T3 floor ---

    def test_t3_floor_when_oi_present_but_below_t2(self):
        """Symbol with oi=200 (below T2=500) falls cleanly to T3."""
        q = _Quote("RIVN", last_price=12.0, average_volume=3_000_000, open_interest=200)
        assert self._classify(q, _make_thresh()) == 3

    def test_t3_for_low_vol_symbol(self):
        """Low-vol symbol always T3 regardless of price/oi."""
        q = _Quote("XYZ", last_price=20.0, average_volume=100_000, open_interest=5_000)
        assert self._classify(q, _make_thresh()) == 3


# ---------------------------------------------------------------------------
# 3. main._stamp_oi helper
# ---------------------------------------------------------------------------

class TestStampOi:
    """
    Tests for main._stamp_oi(quotes, oi_map).
    """

    def _stamp_oi(self, quotes, oi_map):
        from main import _stamp_oi
        return _stamp_oi(quotes, oi_map)

    def test_stamps_correct_oi_from_map(self):
        """Each quote gets its symbol's OI from the map."""
        quotes = [
            _Quote("AAPL", open_interest=0),
            _Quote("TSLA", open_interest=0),
            _Quote("SPY",  open_interest=0),
        ]
        oi_map = {"AAPL": 2000, "TSLA": 800, "SPY": 5000}
        self._stamp_oi(quotes, oi_map)
        assert quotes[0].open_interest == 2000
        assert quotes[1].open_interest == 800
        assert quotes[2].open_interest == 5000

    def test_missing_ticker_gets_zero(self):
        """Symbol absent from oi_map gets open_interest=0."""
        quotes = [_Quote("UNKNOWN", open_interest=999)]
        oi_map = {"AAPL": 2000}
        self._stamp_oi(quotes, oi_map)
        assert quotes[0].open_interest == 0

    def test_mutates_in_place(self):
        """_stamp_oi returns None and mutates quotes list in place."""
        quotes = [_Quote("AAPL", open_interest=0)]
        oi_map = {"AAPL": 1500}
        result = self._stamp_oi(quotes, oi_map)
        assert result is None
        assert quotes[0].open_interest == 1500

    def test_empty_quotes_is_noop(self):
        """Empty quote list does not raise."""
        self._stamp_oi([], {"AAPL": 100})   # must not raise

    def test_empty_oi_map_zeros_all(self):
        """All quotes get 0 when oi_map is empty."""
        quotes = [_Quote("AAPL", open_interest=500), _Quote("TSLA", open_interest=300)]
        self._stamp_oi(quotes, {})
        assert all(q.open_interest == 0 for q in quotes)


# ---------------------------------------------------------------------------
# 4. Integration: OI stamp drives real tier demotion
# ---------------------------------------------------------------------------

class TestOiDrivenTierIntegration:
    """
    End-to-end unit integration: simulates the lifespan() sequence
    (get_oi_map -> _stamp_oi -> assign_tiers) without network/DB.
    Asserts that OI values from the registry correctly gate T1/T2 promotion.
    """

    @pytest.mark.asyncio
    async def test_oi_drives_t1_demotion_to_t3(self):
        """
        Scenario: AAPL has great vol+price but chain OI is 0 (no contracts loaded).
        After stamp+assign_tiers, AAPL must be T3, not T1.
        """
        from services.tier_engine import assign_tiers
        from main import _stamp_oi

        quotes = [_Quote("AAPL", last_price=180.0, average_volume=30_000_000, open_interest=0)]
        oi_map = {"AAPL": 0}   # registry reported no contracts loaded

        _stamp_oi(quotes, oi_map)

        with patch("services.tier_engine._fetch_thresholds", new=AsyncMock(return_value=_make_thresh())):
            tiers = await assign_tiers(quotes)

        assert tiers["AAPL"] == 3, (
            f"AAPL should be T3 (oi=0, no grace) but got T{tiers['AAPL']}"
        )

    @pytest.mark.asyncio
    async def test_oi_drives_correct_t1_promotion(self):
        """
        Scenario: AAPL has great vol+price AND chain OI=2000 >= t1_min_oi=1000.
        Must be classified T1.
        """
        from services.tier_engine import assign_tiers
        from main import _stamp_oi

        quotes = [_Quote("AAPL", last_price=180.0, average_volume=30_000_000, open_interest=0)]
        oi_map = {"AAPL": 2000}

        _stamp_oi(quotes, oi_map)

        with patch("services.tier_engine._fetch_thresholds", new=AsyncMock(return_value=_make_thresh())):
            tiers = await assign_tiers(quotes)

        assert tiers["AAPL"] == 1

    @pytest.mark.asyncio
    async def test_mixed_oi_produces_mixed_tiers(self):
        """
        Three symbols: one qualifies T1, one T2, one T3 based solely on OI.
        """
        from services.tier_engine import assign_tiers
        from main import _stamp_oi

        quotes = [
            _Quote("SPY",  last_price=500.0, average_volume=50_000_000, open_interest=0),  # -> T1
            _Quote("HOOD", last_price=15.0,  average_volume=3_000_000,  open_interest=0),  # -> T2
            _Quote("SPCE", last_price=2.0,   average_volume=200_000,    open_interest=0),  # -> T3
        ]
        oi_map = {
            "SPY":  5_000,  # >= t1_min_oi=1000
            "HOOD": 600,    # >= t2_min_oi=500, < t1_min_oi
            "SPCE": 50,     # < t3_min_oi=100 but T3 is the floor anyway
        }

        _stamp_oi(quotes, oi_map)

        with patch("services.tier_engine._fetch_thresholds", new=AsyncMock(return_value=_make_thresh())):
            tiers = await assign_tiers(quotes)

        assert tiers["SPY"]  == 1, f"SPY should be T1 but got T{tiers['SPY']}"
        assert tiers["HOOD"] == 2, f"HOOD should be T2 but got T{tiers['HOOD']}"
        assert tiers["SPCE"] == 3, f"SPCE should be T3 but got T{tiers['SPCE']}"

    @pytest.mark.asyncio
    async def test_preliminary_vs_final_tier_diff(self):
        """
        Regression: the preliminary tier assignment (OI=0, before stamp)
        differs from the final (OI stamped). Ensures the two-pass design
        in lifespan() produces a different, more accurate result.
        """
        from services.tier_engine import assign_tiers
        from main import _stamp_oi

        quotes = [
            _Quote("NVDA", last_price=900.0, average_volume=25_000_000, open_interest=0),
        ]

        # Pass 1: preliminary with OI=0 (what old code did permanently)
        with patch("services.tier_engine._fetch_thresholds", new=AsyncMock(return_value=_make_thresh())):
            prelim_tiers = await assign_tiers(quotes)

        # Pass 2: stamp real OI then re-classify
        _stamp_oi(quotes, {"NVDA": 3_000})
        with patch("services.tier_engine._fetch_thresholds", new=AsyncMock(return_value=_make_thresh())):
            final_tiers = await assign_tiers(quotes)

        assert prelim_tiers["NVDA"] == 3, "Preliminary pass (oi=0) should yield T3"
        assert final_tiers["NVDA"]  == 1, "Final pass (oi=3000) should yield T1"
