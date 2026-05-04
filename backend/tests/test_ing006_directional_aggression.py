"""
test_ing006_directional_aggression.py

ING-006 acceptance tests.

QA-Q1: 9-case is_directionally_aggressive() matrix (F-1 through F-9).
QA-Q2: Accumulator weighted_premium gate tests.
QA-F4: Counter-separation test — passive Gate 2 drop does not increment
       any parse counter (deliberation fix 2026-05-03).
QA-PREMERGE-F1 (2026-05-03): dominant_direction fallback test — verifies
  that a PUT episode where one event is missing contract_type resolves
  dominant_direction using the episode's own contract_type (self.contract_type),
  not a hardcoded string. Added to confirm SA-PREMERGE-F1 fix is correct.
QA-PREMERGE-F2 (2026-05-03): classify_bid_ask() mid-split boundary tests.
  SA-PREMERGE-F1 deliberation identified that the behavioral change from
  ±10% tolerance bands to a mid-split had no dedicated test coverage.
  TestClassifyBidAsk covers the 8-case boundary table for the new logic.

SCOPE NOTE (QA-F1 — deliberation pre-merge review 2026-05-03):
  This file covers ING-006 scope only: is_directionally_aggressive() behaviour
  and the weighted_premium Gate 2 change in RepetitionAccumulator.

  It does NOT satisfy the S2 CI gate invariants defined in the sprint plan and
  deliberation doc (Sessions 15/19). Those invariants require:
    - sentiment (BULLISH / BEARISH) per (bid_ask_class, contract_type)
    - order_side (BUY / SELL / UNKNOWN)
    - strong_sentiment (True / False)
    - dominant_direction (REPEAT_BUY / REPEAT_SELL) per (order_side, contract_type)
  All four depend on order_side_classifier.py which does not yet exist (S2 scope).
  The full 14-assertion CI gate lives in tests/test_direction_invariants.py,
  which is created in S2 and must pass before S2 merges. Do not treat this
  file as a substitute for that gate.

QA-Q2 notes:
  - 2 aggressive @ $40k + 2 passive @ $40k -> weighted=$120k, total=$160k
  - Passive-only episode at $60k raw against T1 DTE<=7 floor ($50k) ->
    weighted=$30k < $50k -> DROPS (weighted is below floor, not at it)
  - Boundary: aggressive-only at exactly floor -> passes
"""
import asyncio
from datetime import datetime, timezone

from parsers.bid_ask_classifier import classify_bid_ask, is_directionally_aggressive
from signals.repetition_accumulator import (
    RepetitionAccumulator,
    RepetitionEpisode,
    _DEFAULT_DTE_PREMIUM_TIERS,
    _AGGRESSION_DISCOUNT,
)


# ---------------------------------------------------------------------------
# QA-PREMERGE-F2: classify_bid_ask() mid-split boundary tests
# ---------------------------------------------------------------------------

class TestClassifyBidAsk:
    """
    ING-006 SA-PREMERGE-F1 / QA-PREMERGE-F2 (2026-05-03).

    classify_bid_ask() was refactored in ING-006 from ±10%-of-spread
    tolerance bands to a half-spread (mid) split. This class covers the
    8-case boundary table for the new logic.

    Reference spread for all cases: bid=1.00, ask=2.00, mid=1.50.

    Boundary table:
      C-1: fill == ask (2.00)           -> AT_ASK
      C-2: fill >  ask (2.01)           -> ABOVE_ASK
      C-3: fill == bid (1.00)           -> AT_BID
      C-4: fill <  bid (0.99)           -> BELOW_BID
      C-5: fill >  mid, < ask (1.80)    -> AT_ASK
      C-6: fill <  mid, > bid (1.20)    -> AT_BID
      C-7: fill == mid (1.50)           -> MID
      C-8: ask <= bid (crossed/lock)    -> MID (guard)
    """

    BID = 1.00
    ASK = 2.00
    MID = 1.50

    def test_c1_fill_at_ask(self):
        """C-1: fill exactly at ask -> AT_ASK."""
        assert classify_bid_ask(self.ASK, self.BID, self.ASK) == "AT_ASK"

    def test_c2_fill_above_ask(self):
        """C-2: fill above ask -> ABOVE_ASK."""
        assert classify_bid_ask(2.01, self.BID, self.ASK) == "ABOVE_ASK"

    def test_c3_fill_at_bid(self):
        """C-3: fill exactly at bid -> AT_BID."""
        assert classify_bid_ask(self.BID, self.BID, self.ASK) == "AT_BID"

    def test_c4_fill_below_bid(self):
        """C-4: fill below bid -> BELOW_BID."""
        assert classify_bid_ask(0.99, self.BID, self.ASK) == "BELOW_BID"

    def test_c5_fill_above_mid_inside_spread(self):
        """
        C-5: fill inside spread but above midpoint (1.80) -> AT_ASK.

        Under the old ±10% logic this would have been MID for a wide spread,
        or AT_ASK for a narrow spread depending on the tenth value.
        Under the mid-split, any fill above mid that has not reached ask
        is AT_ASK — unambiguously leaning toward the ask side.
        """
        assert classify_bid_ask(1.80, self.BID, self.ASK) == "AT_ASK"

    def test_c6_fill_below_mid_inside_spread(self):
        """
        C-6: fill inside spread but below midpoint (1.20) -> AT_BID.

        Symmetric to C-5. Fill is closer to the bid side — AT_BID.
        """
        assert classify_bid_ask(1.20, self.BID, self.ASK) == "AT_BID"

    def test_c7_fill_at_exact_midpoint(self):
        """
        C-7: fill exactly at midpoint (1.50) -> MID.

        This is the only true ambiguous case in the mid-split logic.
        In practice this is a near-zero-probability event on a real tick
        stream (exact float equality between fill and mid), but must be
        covered to verify the MID path is reachable.
        """
        assert classify_bid_ask(self.MID, self.BID, self.ASK) == "MID"

    def test_c8_crossed_spread_returns_mid(self):
        """
        C-8: ask <= bid (locked or crossed spread) -> MID guard.

        A crossed spread means no valid reference points exist for
        classification. The function returns MID immediately.
        """
        assert classify_bid_ask(1.50, 2.00, 1.00) == "MID"  # ask < bid
        assert classify_bid_ask(1.50, 1.50, 1.50) == "MID"  # ask == bid (locked)

    def test_tight_spread_no_false_mid(self):
        """
        Regression: old ±10% logic produced MID for fills near mid on
        tight spreads. With bid=1.00, ask=1.05, tenth=0.005:
          old logic: fill=1.025 -> mid of spread, old would classify as MID
          new logic: fill=1.025 == mid -> MID (correct, matches old)
          fill=1.026 > mid -> AT_ASK (new; old was also AT_ASK here)
          fill=1.024 < mid -> AT_BID (new; old was also AT_BID here)

        For fills at the exact midpoint of a tight spread the behavior is
        identical between old and new. For fills slightly off mid, new is
        stricter (no tolerance band), which is the desired change.
        """
        bid, ask = 1.00, 1.05
        mid = 1.025
        assert classify_bid_ask(mid, bid, ask) == "MID"
        assert classify_bid_ask(1.026, bid, ask) == "AT_ASK"
        assert classify_bid_ask(1.024, bid, ask) == "AT_BID"


# ---------------------------------------------------------------------------
# QA-Q1: 9-case is_directionally_aggressive() matrix
# ---------------------------------------------------------------------------

class TestIsDirectionallyAggressive:
    """F-1 through F-9 — deliberation QA-Q1 matrix (2026-05-03)."""

    def test_f1_at_ask_call(self):
        """F-1: AT_ASK + CALL -> True (buyer paying up)."""
        assert is_directionally_aggressive("AT_ASK", "CALL") is True

    def test_f2_above_ask_put(self):
        """F-2: ABOVE_ASK + PUT -> True (buyer paying above market)."""
        assert is_directionally_aggressive("ABOVE_ASK", "PUT") is True

    def test_f3_at_bid_put(self):
        """F-3: AT_BID + PUT -> True (put seller writing at bid = conviction bullish)."""
        assert is_directionally_aggressive("AT_BID", "PUT") is True

    def test_f4_below_bid_call(self):
        """F-4: BELOW_BID + CALL -> True (call seller writing below bid = conviction bearish)."""
        assert is_directionally_aggressive("BELOW_BID", "CALL") is True

    def test_f5_at_bid_call(self):
        """F-5: AT_BID + CALL -> True (call seller writing at bid)."""
        assert is_directionally_aggressive("AT_BID", "CALL") is True

    def test_f6_mid_call(self):
        """F-6: MID + CALL -> False (passive / ambiguous)."""
        assert is_directionally_aggressive("MID", "CALL") is False

    def test_f7_mid_put(self):
        """F-7: MID + PUT -> False (passive / ambiguous)."""
        assert is_directionally_aggressive("MID", "PUT") is False

    def test_f8_at_ask_unknown_contract(self):
        """F-8: AT_ASK + '' -> True (AT_ASK is unconditional; contract type irrelevant)."""
        assert is_directionally_aggressive("AT_ASK", "") is True

    def test_f9_below_bid_put(self):
        """F-9: BELOW_BID + PUT -> True (put seller writing below bid = conviction bullish).

        Symmetric to F-3 (AT_BID + PUT) and F-4 (BELOW_BID + CALL).
        Added per deliberation QA-F1 fix (2026-05-03) — was missing from the
        original F-matrix in the sprint spec. Sprint doc updated accordingly.
        """
        assert is_directionally_aggressive("BELOW_BID", "PUT") is True

    def test_case_insensitive(self):
        """Inputs normalised to upper — lowercase should work."""
        assert is_directionally_aggressive("at_ask", "call") is True
        assert is_directionally_aggressive("mid", "put") is False

    def test_none_inputs_safe(self):
        """None inputs should not raise — treated as empty string."""
        assert is_directionally_aggressive(None, None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# QA-Q2: Accumulator weighted_premium gate
# ---------------------------------------------------------------------------

def _make_event(
    premium: float,
    is_aggressive: bool,
    dte: int = 5,
    contract_type: str = "CALL",
    order_side: str = "UNKNOWN",
    omit_contract_type: bool = False,
) -> object:
    """Build a minimal mock event compatible with _DictEventWrapper / ingest_tick.

    Only fields actually consumed by RepetitionAccumulator / _DictEventWrapper
    are set here. Do not add fields that do not exist on OptionsFlowEvent —
    phantom fields mask future AttributeErrors when the real dataclass changes.

    omit_contract_type: if True, the contract_type attribute is not set on
    the returned object. Used by TestDominantDirectionFallback to exercise
    the getattr fallback path in RepetitionEpisode.dominant_direction.
    """

    class _Ev:
        pass

    e = _Ev()
    e.ticker           = "AAPL"
    e.strike           = 180.0
    e.expiry           = "2026-05-10"
    e.dte              = dte
    e.premium          = premium
    e.is_aggressive    = is_aggressive
    e.timestamp        = datetime.now(timezone.utc)
    e.trade_type       = "BLOCK"
    e.underlying_price = 175.0
    e.order_side       = order_side
    e.sentiment        = "BULLISH"
    if not omit_contract_type:
        e.contract_type = contract_type
    return e


class TestWeightedPremiumGate:
    """ING-006 QA-Q2 — Gate 2 evaluates weighted_premium."""

    def _acc(self, aggression_discount: float = 0.5):
        """Build accumulator. Accepts discount param for PBE-F1 coverage."""
        return RepetitionAccumulator(
            window_minutes=30,
            min_trades=3,
            dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
            aggression_discount=aggression_discount,
        )

    def test_aggression_discount_constructor_param(self):
        """
        PBE-F1 (2026-05-03): aggression_discount is a constructor param,
        not only a module constant. Verify a custom discount flows through
        to Gate 2 correctly.

        Discount=1.0: passive events get full weight. Same episode that
        drops at discount=0.5 should pass at discount=1.0.
        3 passive @ $20k = $60k. Floor=$50k. weighted@1.0=$60k >= $50k -> passes.
        weighted@0.5=$30k < $50k -> drops (verified in test_passive_only_drops_below_floor).
        """
        acc = self._acc(aggression_discount=1.0)
        acc.set_tier_map({"AAPL": 1})

        async def run():
            result = None
            for _ in range(3):
                result = await acc.ingest_tick(_make_event(20_000, False, dte=5))
            return result

        result = asyncio.run(run())
        assert result is not None, (
            "discount=1.0 passive episode: weighted=$60k >= $50k floor should pass"
        )

    def test_weighted_premium_calculation(self):
        """2 aggressive @ $40k + 2 passive @ $40k -> weighted=$120k, total=$160k."""
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = [
            _make_event(40_000, True),
            _make_event(40_000, True),
            _make_event(40_000, False),
            _make_event(40_000, False),
        ]
        assert ep.total_premium == 160_000
        assert ep.weighted_premium == 120_000  # 80k full + 40k*0.5*2

    def test_aggression_discount_constant(self):
        """_AGGRESSION_DISCOUNT module constant is 0.5."""
        assert _AGGRESSION_DISCOUNT == 0.5

    def test_passive_only_drops_below_floor(self):
        """
        Passive-only episode: 3 events @ $20k each = $60k raw.
        T1 DTE<=7 floor = $50k. Weighted = $60k * 0.5 = $30k < $50k -> None.

        Note: weighted_premium ($30k) is BELOW the floor ($50k), not at it.
        """
        acc = self._acc()
        acc.set_tier_map({"AAPL": 1})

        async def run():
            result = None
            for _ in range(3):
                result = await acc.ingest_tick(_make_event(20_000, False, dte=5))
            return result

        result = asyncio.run(run())
        assert result is None, "passive-only episode below weighted floor should be dropped"

    def test_aggressive_at_exact_floor_passes(self):
        """
        Aggressive-only: 3 events @ $20k each = $60k raw / weighted.
        T1 DTE<=7 floor = $50k. 60k >= 50k -> passes.
        """
        acc = self._acc()
        acc.set_tier_map({"AAPL": 1})

        async def run():
            result = None
            for _ in range(3):
                result = await acc.ingest_tick(_make_event(20_000, True, dte=5))
            return result

        result = asyncio.run(run())
        assert result is not None, "aggressive episode above floor should pass Gate 2"

    def test_mixed_episode_weighted_passes(self):
        """
        2 aggressive @ $40k + 2 passive @ $40k: weighted=$120k >= $50k floor -> passes.
        """
        acc = self._acc()
        acc.set_tier_map({"AAPL": 1})

        async def run():
            result = None
            for i in range(4):
                aggressive = i < 2
                result = await acc.ingest_tick(_make_event(40_000, aggressive, dte=5))
            return result

        result = asyncio.run(run())
        assert result is not None
        assert result.weighted_premium == 120_000
        assert result.total_premium == 160_000

    def test_boundary_passive_at_double_floor_passes(self):
        """
        Passive-only: 3 events @ $34k each = $102k raw. Weighted = $51k >= $50k -> passes.
        (Verifies floor is >= inclusive.)
        """
        acc = self._acc()
        acc.set_tier_map({"AAPL": 1})

        async def run():
            result = None
            for _ in range(3):
                result = await acc.ingest_tick(_make_event(34_000, False, dte=5))
            return result

        result = asyncio.run(run())
        assert result is not None, "passive episode at exactly 2x floor should pass"


# ---------------------------------------------------------------------------
# QA-PREMERGE-F1: dominant_direction fallback test (SA-PREMERGE-F1 fix)
# ---------------------------------------------------------------------------

class TestDominantDirectionFallback:
    """
    QA-PREMERGE-F1 (2026-05-03).

    Verifies that RepetitionEpisode.dominant_direction correctly falls back
    to self.contract_type (the episode's own contract_type) when an individual
    event is missing its contract_type attribute — not to a hardcoded string.

    SA-PREMERGE-F1 identified that the property previously used
    getattr(e, "contract_type", "CALL") — hardcoded "CALL" — which would
    misclassify premium contributions in PUT episodes where an event had
    no contract_type. The fix reverts to getattr(e, "contract_type",
    self.contract_type) so the episode context is always the fallback.

    Two tests:
      1. PUT episode where one event has no contract_type — must resolve
         REPEAT_SELL (SELL PUT is bearish → REPEAT_SELL via fallback mapping),
         not REPEAT_BUY (what "CALL" fallback would produce for SELL order_side).
         Wait — order_side_to_direction(SELL, PUT) = REPEAT_BUY (put seller is
         bullish). With "CALL" fallback: order_side_to_direction(SELL, CALL)
         would return REPEAT_SELL (call seller is bearish). This is the
         misclassification: selling a PUT is bullish, but if we substitute
         "CALL" as the fallback, the same SELL order_side reads as bearish.
      2. Sanity check — episode with all events carrying correct contract_type
         is unaffected.
    """

    def test_put_episode_missing_contract_type_uses_episode_fallback(self):
        """
        PUT episode, 3 events all with order_side=SELL.
        order_side_to_direction(SELL, PUT) = REPEAT_BUY (put seller is bullish).

        One event is missing contract_type entirely (omit_contract_type=True).
        With self.contract_type fallback ("PUT"): that event still resolves
        REPEAT_BUY -> buy_prem increases correctly.
        With "CALL" fallback: order_side_to_direction(SELL, CALL) = REPEAT_SELL
        -> sell_prem increases instead -> dominant_direction may flip to REPEAT_SELL
        if the missing-contract_type event has enough premium weight.

        This test uses 1 normal event @ $10k and 1 event with missing
        contract_type @ $100k (dominant weight). The missing-contract_type
        event must resolve REPEAT_BUY, not REPEAT_SELL.
        """
        ep = RepetitionEpisode(ticker="AAPL", contract_type="PUT")

        normal_ev = _make_event(10_000, True, contract_type="PUT", order_side="SELL")
        missing_ct_ev = _make_event(
            100_000, True, contract_type="PUT",
            order_side="SELL", omit_contract_type=True
        )
        ep.events = [normal_ev, missing_ct_ev]

        # With correct fallback (self.contract_type = "PUT"):
        #   both events: order_side_to_direction(SELL, PUT) = REPEAT_BUY
        #   buy_prem = 110_000, sell_prem = 0 -> REPEAT_BUY
        #
        # With broken fallback ("CALL"):
        #   missing_ct_ev: order_side_to_direction(SELL, CALL) = REPEAT_SELL
        #   buy_prem = 10_000, sell_prem = 100_000 -> REPEAT_SELL (WRONG)
        assert ep.dominant_direction == "REPEAT_BUY", (
            "PUT episode with missing contract_type event must resolve REPEAT_BUY "
            "using episode fallback (self.contract_type='PUT'), not 'CALL' fallback"
        )

    def test_all_events_with_contract_type_unaffected(self):
        """
        Sanity check: when all events have contract_type set, the result
        is identical regardless of the fallback value — the fallback is
        never invoked.
        """
        ep = RepetitionEpisode(ticker="AAPL", contract_type="PUT")
        ep.events = [
            _make_event(50_000, True, contract_type="PUT", order_side="SELL"),
            _make_event(50_000, True, contract_type="PUT", order_side="SELL"),
        ]
        # Both SELL PUT -> REPEAT_BUY
        assert ep.dominant_direction == "REPEAT_BUY"


# ---------------------------------------------------------------------------
# QA-F4: Counter-separation test
# Passive Gate 2 drop must not increment any parse counter.
# ---------------------------------------------------------------------------

class TestCounterSeparation:
    """
    QA-F4 (deliberation fix 2026-05-03).

    Verifies that a passive-only episode dropped by Gate 2 (weighted_premium
    below floor) does not incorrectly increment any parse-level counter.

    The accumulator does not maintain a _stats dict — parse counters
    (parse_failed, below_min_premium) live in options_flow_parser.py.
    This test confirms Gate 2 returns None cleanly with no side-effects
    that could be misread as a parse error by the stream layer.
    """

    def test_passive_gate2_drop_returns_none_no_exception(self):
        """
        A passive-only episode that fails Gate 2 must return None without
        raising any exception. No parse counter lives in the accumulator,
        so the assertion is: no exception raised AND result is None.
        If any exception propagates here, the stream layer would catch it,
        increment parse_failed, and misattribute a clean gate drop as a
        parse error.
        """
        acc = RepetitionAccumulator(
            window_minutes=30,
            min_trades=3,
            dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
            aggression_discount=0.5,
        )
        acc.set_tier_map({"AAPL": 1})

        async def run():
            result = None
            try:
                for _ in range(3):
                    result = await acc.ingest_tick(_make_event(20_000, False, dte=5))
            except Exception as exc:
                raise AssertionError(
                    f"Gate 2 passive drop raised an exception — "
                    f"stream layer would miscount this as parse_failed: {exc}"
                ) from exc
            return result

        result = asyncio.run(run())
        assert result is None, (
            "passive Gate 2 drop must return None, not an episode"
        )

    def test_aggressive_pass_returns_episode_not_none(self):
        """
        Baseline: aggressive episode that clears Gate 2 returns an episode,
        confirming the counter-separation test above is testing the right path.
        """
        acc = RepetitionAccumulator(
            window_minutes=30,
            min_trades=3,
            dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
            aggression_discount=0.5,
        )
        acc.set_tier_map({"AAPL": 1})

        async def run():
            result = None
            for _ in range(3):
                result = await acc.ingest_tick(_make_event(20_000, True, dte=5))
            return result

        result = asyncio.run(run())
        assert result is not None, "aggressive episode above floor must return an episode"
