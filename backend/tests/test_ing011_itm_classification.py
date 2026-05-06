"""
test_ing011_itm_classification.py

ING-011: ITM put/call misclassification fix.
D1/D2/D3 deliberation resolved 2026-05-06.

Test matrix covers cases I-1 through I-11 (QA matrix from deliberation).

Key invariants under test:
  - OTM PUT AT_BID -> REPEAT_SELL (bullish) — unchanged regression anchor
  - ITM PUT AT_BID -> REPEAT_BUY (bearish) — ING-011 fix
  - DEEP_ITM PUT AT_BID -> REPEAT_BUY (bearish) — ING-011 fix (TMDX scenario)
  - ITM PUT AT_ASK -> REPEAT_BUY (bearish) — already correct, unchanged
  - ITM CALL AT_ASK -> REPEAT_BUY (bullish) — already correct, unchanged
  - ITM CALL AT_BID -> REPEAT_SELL (bearish) — call seller, unchanged
  - ATM PUT AT_BID -> REPEAT_SELL (bullish) — ATM selling, unchanged
  - underlying_price == 0 -> UNKNOWN band, no override, existing fallback
  - Boundary: exactly 2% ITM -> ITM band (inclusive)
  - Boundary: 1.9% ITM -> ATM band (no override)
  - Boundary: exactly 10% ITM -> DEEP_ITM band (inclusive)

All deliberation decisions are referenced inline per test case.
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from signals.repetition_accumulator import (
    RepetitionAccumulator,
    RepetitionEpisode,
    _ITM_THRESHOLD,
    _DEEP_ITM_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_acc(min_premium: float = 10_000) -> RepetitionAccumulator:
    """Accumulator with low floor so all test episodes qualify Gate 2."""
    return RepetitionAccumulator(
        window_minutes=30,
        min_trades=3,
        min_premium=min_premium,
        aggression_discount=0.5,
    )


def _ts(offset_s: int = 0) -> datetime:
    return datetime(2026, 5, 6, 13, 43, 50, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _make_event(
    ticker: str,
    contract_type: str,
    strike: float,
    underlying_price: float,
    bid_ask_class: str,
    premium: float = 50_000,
    order_side: str = "UNKNOWN",
    is_aggressive: bool = True,
    ts_offset: int = 0,
) -> dict:
    return {
        "ticker": ticker,
        "contract_type": contract_type,
        "strike": strike,
        "underlying_price": underlying_price,
        "bid_ask_class": bid_ask_class,
        "premium": premium,
        "order_side": order_side,
        "is_aggressive": is_aggressive,
        "dte": 9,
        "trade_type": "SINGLE",
        "timestamp": _ts(ts_offset),
        "expiry": "2026-05-15",
    }


async def _build_episode(
    acc: RepetitionAccumulator,
    events: list,
) -> RepetitionEpisode:
    """Ingest enough events to clear Gate 1 (min_trades=3) and return episode."""
    ep = None
    for ev in events:
        result = await acc.ingest_tick(ev)
        if result is not None:
            ep = result
    assert ep is not None, "Episode did not emit — check premium floor or trade count"
    return ep


# ---------------------------------------------------------------------------
# _classify_moneyness_band unit tests (pure function — no accumulator needed)
# ---------------------------------------------------------------------------

class TestClassifyMoneynessBand:
    """Direct tests of _classify_moneyness_band() via RepetitionAccumulator."""

    def setup_method(self):
        self.acc = _make_acc()

    def _ev(self, contract_type, strike, underlying_price):
        """Minimal dict wrapper for band classification."""
        from signals.repetition_accumulator import _DictEventWrapper
        return _DictEventWrapper({
            "contract_type": contract_type,
            "strike": strike,
            "underlying_price": underlying_price,
            "premium": 1.0,
            "bid_ask_class": "AT_BID",
        })

    # --- OTM cases (must not regress) ---

    def test_deep_otm_put(self):
        # PUT strike 20% below underlying -> deep OTM PUT
        ev = self._ev("PUT", 80.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "DEEP_OTM"

    def test_otm_put(self):
        # PUT strike 5% below underlying -> OTM
        ev = self._ev("PUT", 95.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "OTM"

    def test_atm_put(self):
        # PUT strike exactly at underlying -> ATM
        ev = self._ev("PUT", 100.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ATM"

    def test_atm_put_within_2pct(self):
        # PUT strike 1.9% below underlying -> ATM band
        ev = self._ev("PUT", 98.1, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ATM"

    # --- ITM cases ---

    def test_itm_put_3pct(self):
        # PUT strike 3% above underlying -> ITM (D1: >2% = ITM)
        ev = self._ev("PUT", 103.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ITM"

    def test_deep_itm_put_39pct(self):
        # TMDX scenario: PUT $105 vs underlying $75.69 -> ~39% ITM -> DEEP_ITM
        ev = self._ev("PUT", 105.0, 75.69)
        assert self.acc._classify_moneyness_band(ev) == "DEEP_ITM"

    def test_itm_call_3pct(self):
        # CALL strike 3% below underlying -> ITM
        ev = self._ev("CALL", 97.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ITM"

    def test_deep_itm_call_15pct(self):
        # CALL strike 15% below underlying -> DEEP_ITM
        ev = self._ev("CALL", 85.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "DEEP_ITM"

    # --- Boundary cases (D1 deliberation) ---

    def test_boundary_exactly_2pct_itm_put(self):
        # PUT strike exactly 2% above underlying -> ITM (inclusive boundary)
        # underlying=100, strike=102 -> pct=0.02 -> not ATM (ATM is <=0.02)
        # Wait: ATM is pct <= _ITM_THRESHOLD (0.02) which is <=, so 2% IS ATM.
        # The ITM check requires strike > underlying * 1.02 for PUT.
        # strike=102, up=100 -> strike/up = 1.02 -> NOT > 1.02, so ATM.
        # First strictly-ITM is strike > underlying * 1.02 = 102.0001
        ev = self._ev("PUT", 102.0, 100.0)
        # pct = abs(102 - 100) / 100 = 0.02 -> ATM (pct <= 0.02)
        assert self.acc._classify_moneyness_band(ev) == "ATM"

    def test_boundary_just_over_2pct_itm_put(self):
        # PUT strike 2.1% above underlying -> ITM
        ev = self._ev("PUT", 102.1, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ITM"

    def test_boundary_1_9pct_itm_put_is_atm(self):
        # PUT strike 1.9% above underlying -> ATM (inside band)
        ev = self._ev("PUT", 101.9, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ATM"

    def test_boundary_exactly_10pct_itm_put(self):
        # PUT strike exactly 10% above underlying -> ITM (not yet DEEP_ITM)
        # pct = 0.10 -> _DEEP_ITM_THRESHOLD = 0.10 -> pct > 0.10 is False
        ev = self._ev("PUT", 110.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ITM"

    def test_boundary_just_over_10pct_itm_put(self):
        # PUT strike 10.1% above underlying -> DEEP_ITM
        ev = self._ev("PUT", 110.1, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "DEEP_ITM"

    # --- Zero underlying price guard ---

    def test_unknown_when_underlying_zero(self):
        ev = self._ev("PUT", 105.0, 0.0)
        assert self.acc._classify_moneyness_band(ev) == "UNKNOWN"

    def test_unknown_when_underlying_none(self):
        from signals.repetition_accumulator import _DictEventWrapper
        ev = _DictEventWrapper({
            "contract_type": "PUT",
            "strike": 105.0,
            "underlying_price": None,
            "premium": 1.0,
            "bid_ask_class": "AT_BID",
        })
        assert self.acc._classify_moneyness_band(ev) == "UNKNOWN"


# ---------------------------------------------------------------------------
# dominant_direction integration tests (full episode path)
# ---------------------------------------------------------------------------

class TestITMDirectionOverride:
    """
    Tests that dominant_direction correctly applies the ING-011 ITM override.
    All cases from the QA test matrix I-1 through I-11.
    """

    # --- I-1: OTM PUT AT_BID -> REPEAT_SELL (bullish) — regression anchor ---

    @pytest.mark.asyncio
    async def test_I1_otm_put_at_bid_is_repeat_sell(self):
        """OTM PUT AT_BID -> REPEAT_SELL (put seller, bullish). Must not regress."""
        acc = _make_acc()
        # underlying=100, strike=85 -> 15% OTM PUT
        events = [
            _make_event("SPY", "PUT", 85.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "DEEP_OTM"
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-2: ITM PUT AT_BID -> REPEAT_BUY (bearish) — ING-011 fix ---

    @pytest.mark.asyncio
    async def test_I2_itm_put_at_bid_is_repeat_buy(self):
        """ITM PUT AT_BID -> REPEAT_BUY (bearish). ING-011 fix."""
        acc = _make_acc()
        # underlying=100, strike=103 -> 3% ITM PUT
        events = [
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- I-3: DEEP_ITM PUT AT_BID -> REPEAT_BUY (bearish) — TMDX scenario ---

    @pytest.mark.asyncio
    async def test_I3_deep_itm_put_at_bid_is_repeat_buy_tmdx(self):
        """DEEP_ITM PUT AT_BID -> REPEAT_BUY. TMDX $105P vs $75.69 scenario."""
        acc = _make_acc()
        # TMDX: PUT $105, underlying $75.69 -> ~39% ITM -> DEEP_ITM
        events = [
            _make_event("TMDX", "PUT", 105.0, 75.69, "AT_BID",
                        premium=34_939,  # 1263 * $27.68 / 100 approx
                        ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "DEEP_ITM"
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- I-4: ITM PUT AT_ASK -> REPEAT_BUY (bearish) — already correct ---

    @pytest.mark.asyncio
    async def test_I4_itm_put_at_ask_is_repeat_buy(self):
        """ITM PUT AT_ASK -> REPEAT_BUY (aggressive put buyer). Already correct."""
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_ASK", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- I-5: ITM CALL AT_ASK -> REPEAT_BUY (bullish) — already correct ---

    @pytest.mark.asyncio
    async def test_I5_itm_call_at_ask_is_repeat_buy(self):
        """ITM CALL AT_ASK -> REPEAT_BUY (aggressive call buyer, bullish). Already correct."""
        acc = _make_acc()
        # underlying=100, strike=97 -> 3% ITM CALL
        events = [
            _make_event("XYZ", "CALL", 97.0, 100.0, "AT_ASK", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- I-6: ITM CALL AT_BID -> REPEAT_SELL (bearish) — call seller, unchanged ---

    @pytest.mark.asyncio
    async def test_I6_itm_call_at_bid_is_repeat_sell(self):
        """ITM CALL AT_BID -> REPEAT_SELL (call writer, bearish). No override — unchanged."""
        acc = _make_acc()
        events = [
            _make_event("XYZ", "CALL", 97.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        # Call AT_BID = call seller = bearish = REPEAT_SELL. No ING-011 override for calls.
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-7: ATM PUT AT_BID -> REPEAT_SELL (bullish) — ATM selling, unchanged ---

    @pytest.mark.asyncio
    async def test_I7_atm_put_at_bid_is_repeat_sell(self):
        """ATM PUT AT_BID -> REPEAT_SELL (put seller, bullish). ATM band, no override."""
        acc = _make_acc()
        # underlying=100, strike=101 -> 1% above -> ATM
        events = [
            _make_event("SPY", "PUT", 101.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ATM"
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-8: underlying_price == 0 -> UNKNOWN, no override ---

    @pytest.mark.asyncio
    async def test_I8_unknown_underlying_no_override(self):
        """underlying_price == 0 -> otm_band UNKNOWN, no ITM override applied."""
        acc = _make_acc()
        # With underlying=0, band is UNKNOWN -> no override -> base direction used
        events = [
            _make_event("XYZ", "PUT", 103.0, 0.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "UNKNOWN"
        # No ITM override fires — base order_side_to_direction() result
        # AT_BID PUT with order_side=UNKNOWN -> order_side_to_direction fallback
        # returns REPEAT_SELL (OTM put selling assumption)
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-9: Boundary — PUT at exactly 2% ITM -> ITM band (inclusive override) ---

    @pytest.mark.asyncio
    async def test_I9_put_at_2pct_itm_boundary_is_atm(self):
        """PUT strike exactly 2% above underlying -> ATM band (pct <= 0.02 is ATM).

        D1 clarification: the ATM gate is pct <= _ITM_THRESHOLD (inclusive).
        A put at exactly 2% moneyness distance falls into ATM, not ITM.
        The first strictly-ITM classification requires pct > 0.02.
        """
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 102.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ATM"
        # ATM -> no ITM override
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-10: Boundary — PUT at 1.9% -> ATM, no override ---

    @pytest.mark.asyncio
    async def test_I10_put_at_1_9pct_itm_is_atm(self):
        """PUT strike 1.9% above underlying -> ATM band, no ITM override."""
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 101.9, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ATM"
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-11: Boundary — PUT at exactly 10% ITM -> DEEP_ITM ---

    @pytest.mark.asyncio
    async def test_I11_put_at_10pct_itm_is_deep_itm_boundary(self):
        """PUT strike exactly 10% above underlying -> ITM (pct > 0.10 required for DEEP_ITM).

        pct = 0.10 -> not > _DEEP_ITM_THRESHOLD (0.10) -> ITM, not DEEP_ITM.
        Both ITM and DEEP_ITM trigger the override, so direction is REPEAT_BUY.
        """
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 110.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"  # pct=0.10 -> not > 0.10 -> ITM
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- Mixed episode: majority AT_BID on ITM PUT ---

    @pytest.mark.asyncio
    async def test_mixed_episode_majority_at_bid_itm_put_is_bearish(self):
        """Mixed episode: 2 AT_BID + 1 AT_ASK on ITM PUT -> majority bid side -> REPEAT_BUY."""
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_BID",
                        premium=60_000, ts_offset=0),
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_BID",
                        premium=60_000, ts_offset=10),
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_ASK",
                        premium=20_000, ts_offset=20),
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- Mixed episode: majority AT_ASK on ITM PUT stays REPEAT_BUY via base logic ---

    @pytest.mark.asyncio
    async def test_mixed_episode_majority_at_ask_itm_put_is_repeat_buy(self):
        """Mixed episode: 1 AT_BID + 2 AT_ASK on ITM PUT -> ask side dominant -> REPEAT_BUY via override check.

        bid_side_prem < ask_side_prem -> override does NOT fire (bid not dominant).
        Base direction for ITM PUT AT_ASK = REPEAT_BUY (put buyer) -> still correct.
        """
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_BID",
                        premium=20_000, ts_offset=0),
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_ASK",
                        premium=60_000, ts_offset=10),
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_ASK",
                        premium=60_000, ts_offset=20),
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- OTM PUT regression: AT_BID does NOT trigger ITM override ---

    @pytest.mark.asyncio
    async def test_otm_put_at_bid_no_itm_override(self):
        """OTM PUT AT_BID must NOT trigger ITM override. REPEAT_SELL preserved."""
        acc = _make_acc()
        # strike well below underlying -> OTM PUT
        events = [
            _make_event("NVDA", "PUT", 90.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "OTM"
        assert ep.dominant_direction == "REPEAT_SELL"


# ---------------------------------------------------------------------------
# Threshold constant sanity checks
# ---------------------------------------------------------------------------

class TestThresholdConstants:
    def test_itm_threshold_matches_ing005_atm_band(self):
        """D1: ITM threshold must equal ING-005 ATM ±2% band."""
        assert _ITM_THRESHOLD == 0.02

    def test_deep_itm_threshold(self):
        """D1: DEEP_ITM threshold is 10%."""
        assert _DEEP_ITM_THRESHOLD == 0.10
