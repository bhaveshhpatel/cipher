"""
test_ing011b_itm_aggression_weight.py

Test matrix for ING-011b: ITM-buyer aggression weight correction.

Deliberation: D1–D5 (SA/PBE/QA 2026-05-06). Issue #80.
Sprint doc commit: 5ba7e7d.

W-1   OTM PUT AT_BID writer — full weight unchanged (ING-006 regression guard)
W-2   ITM PUT AT_BID buyer — discount applies (D1 Option B)
W-3   DEEP_ITM PUT AT_BID buyer — discount applies (D1 Option B)
W-4   ATM PUT AT_BID — NOT discounted (ATM not in _ITM_BANDS)
W-5   ITM CALL AT_BID writer — full weight unchanged (D1 scope gate: PUT only)
W-6   Passive mid-fill (is_aggressive=False) — discount unchanged (ING-006)
W-7   AT_ASK buyer (is_aggressive=True, ITM PUT) — NOT discounted
W-8   UNKNOWN band (underlying_price=0) — D5 fallback, full weight preserved
W-9   Mixed episode: OTM writer + ITM buyer — partial discount, only ITM events
W-10  _classify_moneyness_band module-level: callable, full band enum coverage (D3)
W-11  End-to-end ingest_tick: ITM-put-buyer episode Gate-2 impact
W-12  _majority_itm_band delegates to module-level classify; UNKNOWN events neutral
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta

from signals.repetition_accumulator import (
    RepetitionAccumulator,
    RepetitionEpisode,
    _classify_moneyness_band,
    _AGGRESSION_DISCOUNT,
    _ITM_BANDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_s: int = 0) -> datetime:
    return datetime(2026, 5, 7, 14, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


class _Ev:
    """Minimal event stub for direct RepetitionEpisode method tests."""
    def __init__(
        self,
        premium: float,
        is_aggressive: bool,
        bid_ask_class: str,
        contract_type: str,
        underlying_price: float,
        strike: float,
        order_side: str = "BUY",
        trade_type: str = "BLOCK",
        dte: int = 7,
        ticker: str = "SPY",
        expiry: str = "2026-05-14",
        timestamp: datetime = None,
    ):
        self.premium          = premium
        self.is_aggressive    = is_aggressive
        self.bid_ask_class    = bid_ask_class
        self.contract_type    = contract_type
        self.underlying_price = underlying_price
        self.strike           = strike
        self.order_side       = order_side
        self.trade_type       = trade_type
        self.dte              = dte
        self.ticker           = ticker
        self.expiry           = expiry
        self.timestamp        = timestamp or _ts()


def _episode_with_events(*events) -> RepetitionEpisode:
    """Build a RepetitionEpisode and append events directly."""
    ev0 = events[0]
    ep = RepetitionEpisode(
        ticker=ev0.ticker,
        contract_type=ev0.contract_type,
        strike=ev0.strike,
        expiry=ev0.expiry,
    )
    for e in events:
        ep.events.append(e)
    return ep


DISCOUNT = _AGGRESSION_DISCOUNT  # 0.5


# ---------------------------------------------------------------------------
# W-1: OTM PUT AT_BID writer — full weight (ING-006 regression guard)
# ---------------------------------------------------------------------------
class TestW1OtmPutAtBidWriter:
    """
    OTM PUT AT_BID is_aggressive=True — the ING-006 writer case.
    Must receive full weight (x1.0). D1 Option B must NOT apply here
    (strike < underlying * 0.98 — this is OTM for a put).
    """

    def test_otm_put_at_bid_writer_full_weight(self):
        # PUT, strike=480, underlying=500 — put OTM (strike < underlying)
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=480.0,
        )
        ep = _episode_with_events(ev)
        # OTM PUT AT_BID: is_aggressive=True, band=OTM → full weight
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000.0)

    def test_otm_put_at_bid_writer_not_discounted(self):
        """Confirm classify returns OTM for this event, not ITM."""
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=480.0,
        )
        band = _classify_moneyness_band(ev)
        assert band == "OTM"
        assert band not in _ITM_BANDS


# ---------------------------------------------------------------------------
# W-2: ITM PUT AT_BID buyer — discount applies (D1 Option B)
# ---------------------------------------------------------------------------
class TestW2ItmPutAtBidBuyer:
    """
    ITM PUT AT_BID is_aggressive=True — buyer paying near-intrinsic in wide spread.
    D1 Option B: apply discount regardless of is_aggressive flag.
    strike=520, underlying=500 — PUT is 4% ITM (> _ITM_THRESHOLD=2%).
    """

    def test_itm_put_at_bid_gets_discount(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=520.0,  # 4% ITM
        )
        ep = _episode_with_events(ev)
        expected = 100_000 * DISCOUNT
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(expected)

    def test_itm_put_at_bid_classify_returns_itm(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=520.0,
        )
        assert _classify_moneyness_band(ev) == "ITM"

    def test_itm_put_below_bid_also_discounted(self):
        """BELOW_BID is the other bid-side class — same discount applies."""
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="BELOW_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=520.0,
        )
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000 * DISCOUNT)


# ---------------------------------------------------------------------------
# W-3: DEEP_ITM PUT AT_BID buyer — discount applies (D1 Option B)
# ---------------------------------------------------------------------------
class TestW3DeepItmPutAtBidBuyer:
    """
    DEEP_ITM PUT: strike=560, underlying=500 — PUT is 12% ITM (> _DEEP_ITM_THRESHOLD=10%).
    D1 Option B applies to both ITM and DEEP_ITM bands.
    """

    def test_deep_itm_put_at_bid_discounted(self):
        ev = _Ev(
            premium=200_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=560.0,  # 12% ITM → DEEP_ITM
        )
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(200_000 * DISCOUNT)

    def test_deep_itm_put_classify_returns_deep_itm(self):
        ev = _Ev(
            premium=200_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=560.0,
        )
        assert _classify_moneyness_band(ev) == "DEEP_ITM"


# ---------------------------------------------------------------------------
# W-4: ATM PUT AT_BID — NOT discounted (ATM ∉ _ITM_BANDS)
# ---------------------------------------------------------------------------
class TestW4AtmPutAtBid:
    """
    ATM PUT (|strike - underlying| / underlying <= 0.02) is_aggressive=True.
    ATM is not in _ITM_BANDS — no discount from D1 Option B.
    Full weight applies.
    """

    def test_atm_put_at_bid_full_weight(self):
        # strike=501, underlying=500 — 0.2% → ATM
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=501.0,
        )
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000.0)

    def test_atm_put_classify_returns_atm(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=501.0,
        )
        assert _classify_moneyness_band(ev) == "ATM"


# ---------------------------------------------------------------------------
# W-5: ITM CALL AT_BID writer — full weight unchanged (D1 scope gate: PUT only)
# ---------------------------------------------------------------------------
class TestW5ItmCallAtBidWriter:
    """
    ITM CALL AT_BID: CALL with strike < underlying — call seller = bearish.
    D1 Option B is explicitly gated to PUT only. CALL at ITM AT_BID must
    receive full weight (call seller intent is correct with existing logic).
    """

    def test_itm_call_at_bid_full_weight(self):
        # CALL strike=480, underlying=500 — CALL is 4% ITM
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="CALL",
            underlying_price=500.0,
            strike=480.0,
        )
        ep = _episode_with_events(ev)
        # D1 gate: contract_type must be PUT. CALL → no discount.
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000.0)

    def test_itm_call_classify_returns_itm(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="CALL",
            underlying_price=500.0,
            strike=480.0,
        )
        assert _classify_moneyness_band(ev) == "ITM"


# ---------------------------------------------------------------------------
# W-6: Passive mid-fill (is_aggressive=False) — discount unchanged (ING-006)
# ---------------------------------------------------------------------------
class TestW6PassiveMidFill:
    """
    is_aggressive=False mid-spread fill. ING-006: discount always applies.
    D1 Option B does not alter this path — the is_aggressive=False branch
    is the first else, discount applied unconditionally before any ITM check.
    """

    def test_passive_itm_put_gets_discount(self):
        """Even ITM PUT with is_aggressive=False gets discount (ING-006 path, unchanged)."""
        ev = _Ev(
            premium=100_000,
            is_aggressive=False,
            bid_ask_class="AT_MID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=520.0,  # ITM
        )
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000 * DISCOUNT)

    def test_passive_otm_put_gets_discount(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=False,
            bid_ask_class="AT_MID",
            contract_type="PUT",
            underlying_price=500.0,
            strike=480.0,  # OTM
        )
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000 * DISCOUNT)

    def test_passive_discount_is_ite_006_compliant(self):
        """Verify the discount amount is exactly _AGGRESSION_DISCOUNT (0.5)."""
        ev = _Ev(premium=80_000, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=500.0, strike=480.0)
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(0.5) == pytest.approx(40_000.0)


# ---------------------------------------------------------------------------
# W-7: AT_ASK buyer (is_aggressive=True, ITM PUT) — NOT discounted
# ---------------------------------------------------------------------------
class TestW7ItmPutAtAskBuyer:
    """
    AT_ASK fill on ITM PUT: buyer paying ask in ITM — aggressive buyer intent.
    D1 Option B gate requires bid_ask_class in ('AT_BID','BELOW_BID').
    AT_ASK does not satisfy the gate → full weight.
    """

    def test_itm_put_at_ask_full_weight(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_ASK",
            contract_type="PUT",
            underlying_price=500.0,
            strike=520.0,  # ITM
        )
        ep = _episode_with_events(ev)
        # AT_ASK not in ('AT_BID','BELOW_BID') → no D1 discount
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000.0)

    def test_above_ask_itm_put_full_weight(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="ABOVE_ASK",
            contract_type="PUT",
            underlying_price=500.0,
            strike=520.0,
        )
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000.0)


# ---------------------------------------------------------------------------
# W-8: UNKNOWN band (underlying_price=0) — D5 fallback, full weight
# ---------------------------------------------------------------------------
class TestW8UnknownBandD5Fallback:
    """
    D5 deliberation outcome: when moneyness cannot be determined (underlying_price=0)
    classify returns 'UNKNOWN'. UNKNOWN is not in _ITM_BANDS — no discount.
    Full weight preserved. Safe-by-default: do not penalise undeterminable events.
    """

    def test_unknown_band_no_discount(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=0.0,  # UNKNOWN
            strike=520.0,
        )
        ep = _episode_with_events(ev)
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(100_000.0)

    def test_classify_returns_unknown_for_zero_price(self):
        ev = _Ev(
            premium=100_000,
            is_aggressive=True,
            bid_ask_class="AT_BID",
            contract_type="PUT",
            underlying_price=0.0,
            strike=520.0,
        )
        assert _classify_moneyness_band(ev) == "UNKNOWN"

    def test_unknown_not_in_itm_bands(self):
        assert "UNKNOWN" not in _ITM_BANDS


# ---------------------------------------------------------------------------
# W-9: Mixed episode — partial discount, only ITM events
# ---------------------------------------------------------------------------
class TestW9MixedEpisode:
    """
    Episode with 3 events:
      Ev-A: OTM PUT AT_BID, is_aggressive=True  → full weight (W-1 scenario)
      Ev-B: ITM PUT AT_BID, is_aggressive=True  → discount (W-2 scenario)
      Ev-C: Passive mid-fill, is_aggressive=False → discount (W-6 scenario)

    Total = A_full + B_discounted + C_discounted
    """

    def test_mixed_episode_partial_discount(self):
        ev_a = _Ev(  # OTM PUT AT_BID writer
            premium=60_000, is_aggressive=True, bid_ask_class="AT_BID",
            contract_type="PUT", underlying_price=500.0, strike=480.0,
            timestamp=_ts(0),
        )
        ev_b = _Ev(  # ITM PUT AT_BID buyer
            premium=60_000, is_aggressive=True, bid_ask_class="AT_BID",
            contract_type="PUT", underlying_price=500.0, strike=520.0,
            timestamp=_ts(30),
        )
        ev_c = _Ev(  # Passive mid-fill
            premium=60_000, is_aggressive=False, bid_ask_class="AT_MID",
            contract_type="PUT", underlying_price=500.0, strike=480.0,
            timestamp=_ts(60),
        )
        ep = _episode_with_events(ev_a, ev_b, ev_c)

        expected = (
            60_000           # ev_a: OTM writer, full weight
            + 60_000 * DISCOUNT  # ev_b: ITM buyer, discounted
            + 60_000 * DISCOUNT  # ev_c: passive, discounted
        )
        assert ep.get_weighted_premium(DISCOUNT) == pytest.approx(expected)

    def test_mixed_episode_total_premium_unaffected(self):
        """total_premium is raw sum — unaffected by ING-011b changes."""
        ev_a = _Ev(premium=60_000, is_aggressive=True, bid_ask_class="AT_BID",
                   contract_type="PUT", underlying_price=500.0, strike=480.0)
        ev_b = _Ev(premium=60_000, is_aggressive=True, bid_ask_class="AT_BID",
                   contract_type="PUT", underlying_price=500.0, strike=520.0)
        ep = _episode_with_events(ev_a, ev_b)
        assert ep.total_premium == pytest.approx(120_000.0)


# ---------------------------------------------------------------------------
# W-10: _classify_moneyness_band module-level callable, full band coverage (D3)
# ---------------------------------------------------------------------------
class TestW10ClassifyMoneynessModuleLevel:
    """
    D3 deliberation outcome: _classify_moneyness_band is a module-level function.
    Verify it is importable, callable with an event object, and covers all 6 bands.
    """

    def test_module_level_callable(self):
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=500.0, strike=520.0)
        result = _classify_moneyness_band(ev)
        assert isinstance(result, str)

    def test_deep_itm_put(self):
        # PUT strike=560, underlying=500 — 12% ITM → DEEP_ITM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=500.0, strike=560.0)
        assert _classify_moneyness_band(ev) == "DEEP_ITM"

    def test_itm_put(self):
        # PUT strike=515, underlying=500 — 3% ITM → ITM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=500.0, strike=515.0)
        assert _classify_moneyness_band(ev) == "ITM"

    def test_atm_put(self):
        # PUT strike=501, underlying=500 — 0.2% → ATM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=500.0, strike=501.0)
        assert _classify_moneyness_band(ev) == "ATM"

    def test_otm_put(self):
        # PUT strike=480, underlying=500 — 4% OTM → OTM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=500.0, strike=480.0)
        assert _classify_moneyness_band(ev) == "OTM"

    def test_deep_otm_put(self):
        # PUT strike=430, underlying=500 — 14% OTM → DEEP_OTM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=500.0, strike=430.0)
        assert _classify_moneyness_band(ev) == "DEEP_OTM"

    def test_unknown_zero_price(self):
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="PUT", underlying_price=0.0, strike=520.0)
        assert _classify_moneyness_band(ev) == "UNKNOWN"

    def test_deep_itm_call(self):
        # CALL strike=440, underlying=500 — 12% ITM → DEEP_ITM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="CALL", underlying_price=500.0, strike=440.0)
        assert _classify_moneyness_band(ev) == "DEEP_ITM"

    def test_itm_call(self):
        # CALL strike=485, underlying=500 — 3% ITM → ITM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="CALL", underlying_price=500.0, strike=485.0)
        assert _classify_moneyness_band(ev) == "ITM"

    def test_otm_call(self):
        # CALL strike=520, underlying=500 — 4% OTM → OTM
        ev = _Ev(premium=0, is_aggressive=False, bid_ask_class="AT_MID",
                 contract_type="CALL", underlying_price=500.0, strike=520.0)
        assert _classify_moneyness_band(ev) == "OTM"

    def test_all_bands_in_expected_set(self):
        """Confirm the 6-value band enum is stable."""
        valid = {"DEEP_ITM", "ITM", "ATM", "OTM", "DEEP_OTM", "UNKNOWN"}
        assert _ITM_BANDS == frozenset({"ITM", "DEEP_ITM"})
        assert _ITM_BANDS.issubset(valid)


# ---------------------------------------------------------------------------
# W-11: End-to-end ingest_tick — ITM-put-buyer Gate-2 impact
# ---------------------------------------------------------------------------
class TestW11EndToEndIngestTick:
    """
    Verifies the Gate-2 impact of the D1 discount end-to-end through
    RepetitionAccumulator.ingest_tick().

    Setup: accumulator with low min_premium floor (25_000 for tier-2/DTE<=7).
    min_trades=3. Three ITM PUT AT_BID events, each $60k premium.

    Before ING-011b: weighted_premium = 3 * 60_000 = 180_000 (all full weight).
    After ING-011b:  weighted_premium = 3 * 60_000 * 0.5 = 90_000 (all discounted).

    Gate-2 floor for tier-2, DTE=7: 25_000. Both 180_000 and 90_000 clear 25_000,
    so the episode still emits — but the premium figure is correctly lower.

    A second scenario with a high floor (150_000) confirms the discounted
    weighted_premium (90_000) fails Gate-2 while undiscounted total (180_000)
    would have passed — demonstrating the fix prevents false Gate-2 clears.
    """

    def _make_tick(self, n: int) -> dict:
        return {
            "ticker": "SPY",
            "contract_type": "PUT",
            "strike": 520.0,
            "expiry": "2026-05-14",
            "underlying_price": 500.0,
            "premium": 60_000,
            "is_aggressive": True,
            "bid_ask_class": "AT_BID",
            "order_side": "BUY",
            "trade_type": "BLOCK",
            "dte": 7,
            "timestamp": _ts(n * 30),
        }

    def test_itm_put_buyer_episode_emits_with_low_floor(self):
        """With floor=25_000, discounted weighted_premium (90_000) still clears."""
        acc = RepetitionAccumulator(
            min_trades=3,
            min_premium=25_000,
            aggression_discount=0.5,
            min_sweeps=0,
        )
        acc.set_tier_map({"SPY": 2})
        ep = None
        for i in range(3):
            ep = asyncio.get_event_loop().run_until_complete(
                acc.ingest_tick(self._make_tick(i))
            )
        assert ep is not None, "Episode should emit with floor=25_000"
        assert ep.get_weighted_premium(0.5) == pytest.approx(90_000.0)

    def test_itm_put_buyer_blocked_with_high_floor(self):
        """With floor=150_000, discounted weighted_premium (90_000) fails Gate-2.
        total_premium=180_000 would have passed before ING-011b — this confirms
        the fix prevents false Gate-2 clears on ITM-put-buyer episodes.
        """
        acc = RepetitionAccumulator(
            min_trades=3,
            min_premium=150_000,
            aggression_discount=0.5,
            min_sweeps=0,
        )
        ep = None
        for i in range(3):
            ep = asyncio.get_event_loop().run_until_complete(
                acc.ingest_tick(self._make_tick(i))
            )
        assert ep is None, (
            "Episode should NOT emit: discounted weighted_premium (90_000) < floor (150_000). "
            "Before ING-011b, total_premium (180_000) would have passed falsely."
        )

    def test_otm_put_writer_unaffected_with_same_floor(self):
        """OTM PUT AT_BID writer: weighted_premium=180_000 (no discount) clears 150_000 floor."""
        acc = RepetitionAccumulator(
            min_trades=3,
            min_premium=150_000,
            aggression_discount=0.5,
            min_sweeps=0,
        )

        def _otm_tick(n: int) -> dict:
            return {
                "ticker": "SPY",
                "contract_type": "PUT",
                "strike": 480.0,   # OTM put
                "expiry": "2026-05-14",
                "underlying_price": 500.0,
                "premium": 60_000,
                "is_aggressive": True,
                "bid_ask_class": "AT_BID",
                "order_side": "SELL",
                "trade_type": "BLOCK",
                "dte": 7,
                "timestamp": _ts(n * 30),
            }

        ep = None
        for i in range(3):
            ep = asyncio.get_event_loop().run_until_complete(
                acc.ingest_tick(_otm_tick(i))
            )
        assert ep is not None, "OTM PUT writer episode should still emit (W-1 regression)"
        assert ep.get_weighted_premium(0.5) == pytest.approx(180_000.0)


# ---------------------------------------------------------------------------
# W-12: _majority_itm_band delegates to module-level classify; UNKNOWN neutral
# ---------------------------------------------------------------------------
class TestW12MajorityItmBand:
    """
    _majority_itm_band() must delegate to module-level _classify_moneyness_band()
    (ING-011b D3 — eliminates inline duplication from PBE-6/QA-F1).

    UNKNOWN events (underlying_price=0) must contribute 0 weight to both sides
    (SA-F1 neutral rule). A single UNKNOWN final tick must not suppress the
    override when prior ITM ticks dominate by premium.
    """

    def test_itm_majority_returns_true(self):
        """3 ITM events > 0 OTM events → _majority_itm_band() = True."""
        ev1 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                  contract_type="PUT", underlying_price=500.0, strike=520.0)
        ev2 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                  contract_type="PUT", underlying_price=500.0, strike=520.0)
        ev3 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                  contract_type="PUT", underlying_price=500.0, strike=520.0)
        ep = _episode_with_events(ev1, ev2, ev3)
        assert ep._majority_itm_band() is True

    def test_otm_majority_returns_false(self):
        """3 OTM events > 0 ITM events → _majority_itm_band() = False."""
        ev1 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                  contract_type="PUT", underlying_price=500.0, strike=480.0)
        ev2 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                  contract_type="PUT", underlying_price=500.0, strike=480.0)
        ep = _episode_with_events(ev1, ev2)
        assert ep._majority_itm_band() is False

    def test_unknown_final_tick_does_not_suppress_itm_majority(self):
        """
        SA-F1 regression: final tick with underlying_price=0 (UNKNOWN) must not
        suppress the override when prior ticks are ITM by premium.
        UNKNOWN contributes 0 to both sides — neutral.
        """
        ev_itm1 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                      contract_type="PUT", underlying_price=500.0, strike=520.0,
                      timestamp=_ts(0))
        ev_itm2 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                      contract_type="PUT", underlying_price=500.0, strike=520.0,
                      timestamp=_ts(30))
        ev_unknown = _Ev(premium=50_000, is_aggressive=True, bid_ask_class="AT_BID",
                         contract_type="PUT", underlying_price=0.0, strike=520.0,
                         timestamp=_ts(60))
        ep = _episode_with_events(ev_itm1, ev_itm2, ev_unknown)
        # itm_prem=200_000, non_itm_prem=0, unknown neutral → True
        assert ep._majority_itm_band() is True

    def test_pure_unknown_episode_returns_false(self):
        """All underlying_price=0 → itm_prem=0, non_itm_prem=0 → 0 > 0 is False."""
        ev1 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                  contract_type="PUT", underlying_price=0.0, strike=520.0)
        ev2 = _Ev(premium=100_000, is_aggressive=True, bid_ask_class="AT_BID",
                  contract_type="PUT", underlying_price=0.0, strike=520.0)
        ep = _episode_with_events(ev1, ev2)
        assert ep._majority_itm_band() is False

    def test_mixed_itm_otm_premium_weighted(self):
        """
        ITM premium dominates by weight even though OTM has more event count.
        2 OTM events @ $40k each = $80k non-ITM
        1 ITM event  @ $90k      = $90k ITM
        → ITM wins by premium → True.
        """
        ev_otm1 = _Ev(premium=40_000, is_aggressive=True, bid_ask_class="AT_BID",
                      contract_type="PUT", underlying_price=500.0, strike=480.0)
        ev_otm2 = _Ev(premium=40_000, is_aggressive=True, bid_ask_class="AT_BID",
                      contract_type="PUT", underlying_price=500.0, strike=480.0)
        ev_itm  = _Ev(premium=90_000, is_aggressive=True, bid_ask_class="AT_BID",
                      contract_type="PUT", underlying_price=500.0, strike=520.0)
        ep = _episode_with_events(ev_otm1, ev_otm2, ev_itm)
        assert ep._majority_itm_band() is True
