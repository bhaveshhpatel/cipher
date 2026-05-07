"""
test_ing011_itm_classification.py

ING-011: ITM put/call misclassification fix.
D1/D2/D3 deliberation resolved 2026-05-06.

Test matrix covers cases I-1 through I-12 (QA matrix from deliberation +
QA-F1 panel finding).

Key invariants under test:
  - OTM PUT AT_BID -> REPEAT_SELL (bullish) — unchanged regression anchor
  - ITM PUT AT_BID -> REPEAT_BUY (bearish) — ING-011 fix
  - DEEP_ITM PUT AT_BID -> REPEAT_BUY (bearish) — ING-011 fix (TMDX scenario)
  - ITM PUT AT_ASK + order_side='BUY' -> REPEAT_SELL (bearish) — cipher
    semantic: BUY PUT = REPEAT_SELL; override does not fire (ask dominant)
  - ITM CALL AT_ASK -> REPEAT_BUY (bullish) — already correct, unchanged
  - ITM CALL AT_BID -> REPEAT_SELL (bearish) — call seller, unchanged
  - ATM PUT AT_BID -> REPEAT_SELL (bullish) — ATM selling, unchanged
  - underlying_price == 0 -> UNKNOWN band, no override, existing fallback
  - Boundary: exactly 2% ITM -> ATM band (pct <= 0.02 is ATM, inclusive)
  - Boundary: 1.9% ITM -> ATM band (no override)
  - Boundary: exactly 10% ITM -> ITM band (pct > 0.10 required for DEEP_ITM)
  - SA-F1: UNKNOWN final tick does NOT suppress ITM override when prior
    ticks established a clear ITM/DEEP_ITM premium majority

All deliberation decisions are referenced inline per test case.

Note on dominant_direction fallback:
  _make_event() uses order_side='UNKNOWN'. The fallback order_side_to_direction
  for ('UNKNOWN', 'PUT') returns REPEAT_SELL, so buy_prem=0 in the
  premium-weighting loop of dominant_direction. base_direction='REPEAT_SELL'.
  The ITM override fires only when bid_side_prem > ask_side_prem, flipping
  direction to REPEAT_BUY for ITM/DEEP_ITM PUT + AT_BID.

Cipher direction semantics (see order_side_classifier.py):
  BUY  + CALL -> REPEAT_BUY    (bullish)
  SELL + PUT  -> REPEAT_BUY    (put-selling = bullish positioning)
  BUY  + PUT  -> REPEAT_SELL   (put-buying = bearish)
  SELL + CALL -> REPEAT_SELL   (call-selling = bearish/capped)
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

    def test_boundary_exactly_2pct_itm_put_is_atm(self):
        # PUT strike exactly 2% above underlying -> ATM (pct <= 0.02 is ATM, inclusive)
        # pct = abs(102 - 100) / 100 = 0.02 -> not > _ITM_THRESHOLD -> ATM
        ev = self._ev("PUT", 102.0, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ATM"

    def test_boundary_just_over_2pct_itm_put(self):
        # PUT strike 2.1% above underlying -> ITM
        ev = self._ev("PUT", 102.1, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ITM"

    def test_boundary_1_9pct_itm_put_is_atm(self):
        # PUT strike 1.9% above underlying -> ATM (inside band)
        ev = self._ev("PUT", 101.9, 100.0)
        assert self.acc._classify_moneyness_band(ev) == "ATM"

    def test_boundary_exactly_10pct_itm_put_is_itm(self):
        # PUT strike exactly 10% above underlying -> ITM (pct > 0.10 required for DEEP_ITM)
        # pct = 0.10 -> not > _DEEP_ITM_THRESHOLD (0.10) -> ITM
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
    All cases from the QA test matrix I-1 through I-12.

    Direction mechanics with order_side='UNKNOWN':
      order_side_to_direction('UNKNOWN', 'PUT') -> REPEAT_SELL  (fallback)
      order_side_to_direction('UNKNOWN', 'CALL') -> REPEAT_BUY  (fallback)
    So buy_prem=0, sell_prem=total for all PUT events in the premium-weighting
    loop -> base_direction='REPEAT_SELL' for every PUT episode.
    The ITM override fires when bid_side_prem > ask_side_prem and the episode
    is in the ITM/DEEP_ITM band, flipping direction to REPEAT_BUY (bearish).
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
                        premium=34_939,  # ~1263 contracts * $27.68 / 100
                        ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "DEEP_ITM"
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- I-4: ITM PUT AT_ASK + order_side='BUY' -> REPEAT_SELL ---

    @pytest.mark.asyncio
    async def test_I4_itm_put_at_ask_is_repeat_sell(self):
        """ITM PUT AT_ASK with order_side='BUY' -> REPEAT_SELL (bearish).

        Cipher direction semantic (order_side_classifier.py):
          BUY + PUT -> REPEAT_SELL  (put-buying = bearish positioning)

        The ITM bid-dominance override also does NOT fire here because all
        fills are AT_ASK: ask_side_prem dominates, so the
        bid_side_prem > ask_side_prem condition is false.

        Two independent reasons both produce REPEAT_SELL:
          1. order_side_to_direction('BUY', 'PUT') -> REPEAT_SELL
          2. ask_side dominant -> override gate fails
        """
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 103.0, 100.0, "AT_ASK",
                        order_side="BUY", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        # BUY PUT = bearish = REPEAT_SELL in cipher semantics
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-5: ITM CALL AT_ASK -> REPEAT_BUY (bullish) — already correct ---

    @pytest.mark.asyncio
    async def test_I5_itm_call_at_ask_is_repeat_buy(self):
        """ITM CALL AT_ASK -> REPEAT_BUY (aggressive call buyer, bullish). Already correct."""
        acc = _make_acc()
        # underlying=100, strike=97 -> 3% ITM CALL
        # order_side_to_direction('UNKNOWN', 'CALL') -> REPEAT_BUY (fallback)
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
        """ITM CALL AT_BID -> REPEAT_SELL (call writer, bearish). No ING-011 override.

        Two independent reasons REPEAT_SELL is produced — both must be understood:

        Reason 1 — ING-011 override is structurally gated out by contract type:
          dominant_direction checks `self.contract_type.upper() == 'PUT'` before
          entering the ITM bid-dominance block. A CALL episode never reaches that
          block regardless of bid_ask_class or otm_band. The override is PUT-only
          by design (D2 deliberation 2026-05-06).

        Reason 2 — order_side_to_direction produces the correct result independently:
          order_side='SELL' + contract_type='CALL' -> REPEAT_SELL (call writer,
          bearish). This is the correct existing cipher semantic and requires no
          override. order_side='SELL' is used here to manufacture this path;
          with order_side='UNKNOWN' the fallback would return REPEAT_BUY (call
          buyer), which would be the wrong direction for a call writer scenario.

        The test uses order_side='SELL' to exercise Reason 2. Reason 1 is
        structural and holds for any order_side value on any CALL episode.
        """
        acc = _make_acc()
        events = [
            _make_event("XYZ", "CALL", 97.0, 100.0, "AT_BID",
                        order_side="SELL", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"
        # contract_type=CALL -> ING-011 override structurally absent (Reason 1).
        # order_side='SELL' + CALL -> REPEAT_SELL via order_side_to_direction (Reason 2).
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
        """underlying_price == 0 on ALL events -> no ITM override applied.

        SA-F1 / _majority_itm_band() mechanism:
          All events have underlying_price == 0, so every event is skipped
          in _majority_itm_band() (UNKNOWN events contribute 0 to both sides).
          itm_prem == non_itm_prem == 0.0; 0.0 > 0.0 is False.
          _majority_itm_band() returns False -> override gate never fires.

        base_direction from order_side_to_direction('UNKNOWN', 'PUT')
        = REPEAT_SELL is returned as-is.

        Note: ep.otm_band reflects the last-tick classification (SA-6). Since
        the final tick has underlying_price == 0, ep.otm_band == 'UNKNOWN'.
        The override is not gated on ep.otm_band — it is gated on
        _majority_itm_band() which independently confirms no ITM premium exists.
        """
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 103.0, 0.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "UNKNOWN"
        # _majority_itm_band() returns False (0.0 > 0.0) -> no ITM override fires
        assert ep.dominant_direction == "REPEAT_SELL"

    # --- I-9: Boundary — PUT at exactly 2% above underlying -> ATM (inclusive) ---

    @pytest.mark.asyncio
    async def test_I9_put_at_2pct_itm_boundary_is_atm(self):
        """PUT strike exactly 2% above underlying -> ATM band (pct <= 0.02 is ATM).

        D1 clarification: ATM gate is pct <= _ITM_THRESHOLD (inclusive at 2%).
        A put at exactly 2% moneyness distance falls into ATM, not ITM.
        No ITM override fires -> REPEAT_SELL.
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

    # --- I-11: Boundary — PUT at exactly 10% ITM -> ITM (not DEEP_ITM), override still fires ---

    @pytest.mark.asyncio
    async def test_I11_put_at_10pct_itm_is_itm_override_fires(self):
        """PUT strike exactly 10% above underlying -> ITM (pct > 0.10 required for DEEP_ITM).

        pct = 0.10 -> not > _DEEP_ITM_THRESHOLD (0.10) -> ITM.
        ITM band + AT_BID PUT -> override fires -> REPEAT_BUY.
        """
        acc = _make_acc()
        events = [
            _make_event("XYZ", "PUT", 110.0, 100.0, "AT_BID", ts_offset=i * 10)
            for i in range(3)
        ]
        ep = await _build_episode(acc, events)
        assert ep.otm_band == "ITM"  # pct=0.10 -> not > 0.10 -> ITM
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- I-12: SA-F1 — UNKNOWN final tick does NOT suppress ITM override ---

    @pytest.mark.asyncio
    async def test_I12_unknown_final_tick_does_not_suppress_itm_override(self):
        """SA-F1 canonical scenario: 2 DEEP_ITM events + 1 final UNKNOWN-underlying event.

        SA-F1 fix (panel finding 2026-05-06):
          self.otm_band reflects ONLY the last tick (SA-6 Phase 1 accepted
          limitation). If the final tick has underlying_price == 0, self.otm_band
          would be 'UNKNOWN'. If the override were gated on self.otm_band, a
          final UNKNOWN tick would silently suppress REPEAT_BUY even when all
          prior ticks were clearly DEEP_ITM.

          The fix: dominant_direction calls self._majority_itm_band() which
          computes a premium-weighted majority across ALL episode events.
          UNKNOWN events (underlying_price == 0) contribute 0 weight to both
          sides — they are neutral, not suppressive.

        Episode structure:
          Event 1: TMDX PUT $105, underlying $75.69, AT_BID, premium $34,939 -> DEEP_ITM
          Event 2: TMDX PUT $105, underlying $75.69, AT_BID, premium $34,939 -> DEEP_ITM
          Event 3: TMDX PUT $105, underlying $0.00,  AT_BID, premium $34,939 -> UNKNOWN (final)

        _majority_itm_band() accumulation:
          itm_prem     = 34,939 + 34,939 = 69,878  (events 1 and 2)
          non_itm_prem = 0.0                        (event 3 skipped — UNKNOWN)
          69,878 > 0.0 -> True -> ITM majority confirmed

        Override fires: contract_type=PUT, _majority_itm_band()=True,
        bid_side_prem (104,817) > ask_side_prem (0) -> REPEAT_BUY.

        ep.otm_band reflects the last tick (underlying_price=0) -> 'UNKNOWN'.
        This is correct and expected per SA-6 — otm_band is the reported field,
        not the override gate input.
        """
        acc = _make_acc()
        events = [
            _make_event("TMDX", "PUT", 105.0, 75.69, "AT_BID",
                        premium=34_939, ts_offset=0),
            _make_event("TMDX", "PUT", 105.0, 75.69, "AT_BID",
                        premium=34_939, ts_offset=10),
            # Final tick: underlying_price=0 -> UNKNOWN band -> ep.otm_band='UNKNOWN'
            # _majority_itm_band() skips this event (neutral) — prior ITM premium
            # still dominates. Override must still fire.
            _make_event("TMDX", "PUT", 105.0, 0.0, "AT_BID",
                        premium=34_939, ts_offset=20),
        ]
        ep = await _build_episode(acc, events)
        # ep.otm_band = last-tick classification (SA-6) -> UNKNOWN (underlying_price=0)
        assert ep.otm_band == "UNKNOWN"
        # _majority_itm_band() returns True (69,878 ITM prem > 0 non-ITM) ->
        # override fires despite UNKNOWN last tick -> REPEAT_BUY (bearish)
        assert ep.dominant_direction == "REPEAT_BUY"

    # --- Mixed episode: majority AT_BID on ITM PUT fires override -> REPEAT_BUY ---

    @pytest.mark.asyncio
    async def test_mixed_episode_majority_at_bid_itm_put_is_repeat_buy(self):
        """Mixed episode: 2 AT_BID (120k) + 1 AT_ASK (20k) on ITM PUT.

        bid_side_prem=120k > ask_side_prem=20k -> override fires -> REPEAT_BUY.
        """
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

    # --- Mixed episode: majority AT_ASK on ITM PUT — override does not fire ---

    @pytest.mark.asyncio
    async def test_mixed_episode_majority_at_ask_itm_put_no_override(self):
        """Mixed episode: 1 AT_BID (20k) + 2 AT_ASK (120k) on ITM PUT.

        ask_side_prem=120k > bid_side_prem=20k -> bid NOT dominant -> override
        does NOT fire. base_direction from order_side_to_direction('UNKNOWN', 'PUT')
        = REPEAT_SELL (fallback: buy_prem=0). Returns REPEAT_SELL.
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
        # bid not dominant -> no override -> base REPEAT_SELL
        assert ep.dominant_direction == "REPEAT_SELL"

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
