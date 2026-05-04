"""
test_ing006_directional_aggression.py

ING-006 acceptance tests.

QA-Q1: 8-case is_directionally_aggressive() matrix (F-1 through F-8)
QA-Q2: Accumulator weighted_premium gate test
  - 2 aggressive @ $40k + 2 passive @ $40k -> weighted=$120k, total=$160k
  - Passive-only episode at $50k raw against T1 DTE<=7 floor ($50k) ->
    weighted=$25k -> DROPS (floor is inclusive >=)
  - Boundary: aggressive-only at exactly floor -> passes
"""
import asyncio
from datetime import datetime, timezone

from parsers.bid_ask_classifier import is_directionally_aggressive
from signals.repetition_accumulator import (
    RepetitionAccumulator,
    RepetitionEpisode,
    _DEFAULT_DTE_PREMIUM_TIERS,
    _AGGRESSION_DISCOUNT,
)


# ---------------------------------------------------------------------------
# QA-Q1: 8-case is_directionally_aggressive() matrix
# ---------------------------------------------------------------------------

class TestIsDirectionallyAggressive:
    """F-1 through F-8 — deliberation QA-Q1 matrix (2026-05-03)."""

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

def _make_event(premium: float, is_aggressive: bool, dte: int = 5) -> object:
    """Build a minimal mock event compatible with _DictEventWrapper / ingest_tick."""

    class _Ev:
        pass

    e = _Ev()
    e.ticker           = "AAPL"
    e.contract_type    = "CALL"
    e.strike           = 180.0
    e.expiry           = "2026-05-10"
    e.dte              = dte
    e.premium          = premium
    e.is_aggressive    = is_aggressive
    e.timestamp        = datetime.now(timezone.utc)
    e.trade_type       = "BLOCK"
    e.underlying_price = 175.0
    e.order_side       = "UNKNOWN"
    e.occ_symbol       = None
    e.direction        = None
    e.sentiment        = "BULLISH"
    return e


class TestWeightedPremiumGate:
    """ING-006 QA-Q2 — Gate 2 evaluates weighted_premium."""

    def _acc(self):
        return RepetitionAccumulator(
            window_minutes=30,
            min_trades=3,
            dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
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
        assert ep.weighted_premium == 120_000  # 80k + 40k*0.5*2

    def test_aggression_discount_constant(self):
        """_AGGRESSION_DISCOUNT is 0.5."""
        assert _AGGRESSION_DISCOUNT == 0.5

    def test_passive_only_drops_at_exact_floor(self):
        """
        Passive-only episode: 3 events @ $20k each = $60k raw.
        T1 DTE<=7 floor = $50k. Weighted = 60k * 0.5 = $30k < $50k -> None.
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
                aggressive = i < 2  # first two aggressive, last two passive
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
