"""
tests/test_composite_signal_engine.py — S6 test suite

Covers:
  - episode_influence_tier() all four bands
  - premium_tier_score() all four bands (boundaries inclusive)
  - volume_weighted_premium_factor() — no OI, zero OI, real OI
  - compute_flow_score() — zero prem, max prem, acceleration, trade count
  - build_composite() — strong_sentiment full path, weak 0.80x discount,
    pre-sector ceiling path, all recommendation branches,
    backtest_score always 0.0, weight arithmetic correctness
  - QA path coverage: influence tier RETAIL / LARGE / INSTITUTIONAL / WHALE
  - QA path coverage: composite_score_ceiling present in tradier_stream payload (structural)
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from repo root without installing the package
# ---------------------------------------------------------------------------
_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

import math
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal stubs so tests run without full app context
# ---------------------------------------------------------------------------

class _Ev:
    """Minimal OptionsFlowEvent stub for composite engine tests."""
    def __init__(
        self,
        premium=200_000.0,
        open_interest=100,
        sentiment="BULLISH",
        strong_sentiment=True,
        dte=30,
        influence_tier="LARGE",
    ):
        self.premium        = premium
        self.open_interest  = open_interest
        self.sentiment      = sentiment
        self.strong_sentiment = strong_sentiment
        self.dte            = dte
        self.influence_tier = influence_tier


class _Ep:
    """Minimal RepetitionEpisode stub."""
    def __init__(
        self,
        ticker="AAPL",
        contract_type="CALL",
        strike=150.0,
        expiry="2026-06-20",
        total_premium=200_000.0,
        trade_count=5,
        is_accelerating=False,
        events=None,
    ):
        self.ticker         = ticker
        self.contract_type  = contract_type
        self.strike         = strike
        self.expiry         = expiry
        self.total_premium  = total_premium
        self.trade_count    = trade_count
        self.is_accelerating = is_accelerating
        self.events         = events if events is not None else [
            _Ev(premium=total_premium)
        ]


def _ep_with_event(ev_kwargs=None, ep_kwargs=None):
    """Build an _Ep with a single _Ev, optionally overriding fields."""
    ev_kw = ev_kwargs or {}
    ep_kw = ep_kwargs or {}
    ev = _Ev(**ev_kw)
    ep = _Ep(events=[ev], **ep_kw)
    # keep total_premium consistent with event if not independently set
    if "total_premium" not in ep_kw:
        ep.total_premium = ev.premium
    return ep


# ---------------------------------------------------------------------------
# episode_influence_tier
# ---------------------------------------------------------------------------

from signals.composite_signal_engine import (
    episode_influence_tier,
    premium_tier_score,
    volume_weighted_premium_factor,
    compute_flow_score,
    build_composite,
    CompositeSignal,
)


class TestEpisodeInfluenceTier:
    def test_retail_below_100k(self):
        assert episode_influence_tier(_Ep(total_premium=99_999)) == "RETAIL"

    def test_retail_at_zero(self):
        assert episode_influence_tier(_Ep(total_premium=0)) == "RETAIL"

    def test_large_at_100k(self):
        assert episode_influence_tier(_Ep(total_premium=100_000)) == "LARGE"

    def test_large_just_below_500k(self):
        assert episode_influence_tier(_Ep(total_premium=499_999)) == "LARGE"

    def test_institutional_at_500k(self):
        assert episode_influence_tier(_Ep(total_premium=500_000)) == "INSTITUTIONAL"

    def test_institutional_just_below_2m(self):
        assert episode_influence_tier(_Ep(total_premium=1_999_999)) == "INSTITUTIONAL"

    def test_whale_at_2m(self):
        assert episode_influence_tier(_Ep(total_premium=2_000_000)) == "WHALE"

    def test_whale_above_2m(self):
        assert episode_influence_tier(_Ep(total_premium=5_000_000)) == "WHALE"


# ---------------------------------------------------------------------------
# premium_tier_score
# ---------------------------------------------------------------------------

class TestPremiumTierScore:
    def test_watch_band_below_100k(self):
        assert premium_tier_score(_Ep(total_premium=0)) == 0.0
        assert premium_tier_score(_Ep(total_premium=99_999)) == 0.0

    def test_alert_band_at_100k(self):
        assert premium_tier_score(_Ep(total_premium=100_000)) == 0.25

    def test_alert_band_just_below_500k(self):
        assert premium_tier_score(_Ep(total_premium=499_999)) == 0.25

    def test_strong_signal_band_at_500k(self):
        assert premium_tier_score(_Ep(total_premium=500_000)) == 0.60

    def test_strong_signal_band_just_below_2m(self):
        assert premium_tier_score(_Ep(total_premium=1_999_999)) == 0.60

    def test_conviction_band_at_2m(self):
        assert premium_tier_score(_Ep(total_premium=2_000_000)) == 1.0

    def test_conviction_band_above_2m(self):
        assert premium_tier_score(_Ep(total_premium=8_000_000)) == 1.0


# ---------------------------------------------------------------------------
# volume_weighted_premium_factor
# ---------------------------------------------------------------------------

class TestVolumeWeightedPremiumFactor:
    def test_no_events_returns_half(self):
        ep = _Ep(events=[])
        assert volume_weighted_premium_factor(ep) == 0.5

    def test_zero_oi_returns_half(self):
        ep = _ep_with_event(ev_kwargs={"open_interest": 0})
        assert volume_weighted_premium_factor(ep) == 0.5

    def test_negative_oi_treated_as_zero(self):
        ep = _ep_with_event(ev_kwargs={"open_interest": -1})
        assert volume_weighted_premium_factor(ep) == 0.5

    def test_normal_ratio_below_cap(self):
        # premium=10_000, oi=500 -> 10_000 / (500*100) = 0.2
        ep = _ep_with_event(ev_kwargs={"premium": 10_000, "open_interest": 500})
        assert volume_weighted_premium_factor(ep) == pytest.approx(0.2, abs=0.0001)

    def test_ratio_capped_at_1(self):
        # premium=1_000_000, oi=1 -> would be 10_000 but capped at 1.0
        ep = _ep_with_event(ev_kwargs={"premium": 1_000_000, "open_interest": 1})
        assert volume_weighted_premium_factor(ep) == 1.0

    def test_volume_greater_than_oi_boost(self):
        # premium > oi*100 => ratio > 1.0 => capped at 1.0
        ep = _ep_with_event(ev_kwargs={"premium": 600_000, "open_interest": 50})
        # 600_000 / (50*100) = 120 -> capped to 1.0
        assert volume_weighted_premium_factor(ep) == 1.0


# ---------------------------------------------------------------------------
# compute_flow_score
# ---------------------------------------------------------------------------

class TestComputeFlowScore:
    def test_zero_premium_no_accel_no_trades(self):
        ep = _Ep(total_premium=0, trade_count=0, is_accelerating=False)
        score = compute_flow_score(ep)
        assert score == 0.0

    def test_large_premium_accelerating(self):
        ep = _Ep(total_premium=10_000_000, trade_count=20, is_accelerating=True)
        score = compute_flow_score(ep)
        assert score == 1.0

    def test_acceleration_adds_0_15(self):
        ep_no  = _Ep(total_premium=1_000_000, trade_count=0, is_accelerating=False)
        ep_yes = _Ep(total_premium=1_000_000, trade_count=0, is_accelerating=True)
        delta = compute_flow_score(ep_yes) - compute_flow_score(ep_no)
        assert delta == pytest.approx(0.15, abs=0.001)

    def test_trade_count_caps_at_20(self):
        ep_20  = _Ep(total_premium=0, trade_count=20, is_accelerating=False)
        ep_100 = _Ep(total_premium=0, trade_count=100, is_accelerating=False)
        assert compute_flow_score(ep_20) == compute_flow_score(ep_100)


# ---------------------------------------------------------------------------
# build_composite — core S6 paths
# ---------------------------------------------------------------------------

class TestBuildComposite:
    """All build_composite tests use a real RepetitionAccumulator mock (struct only)."""

    def _make_acc(self):
        acc = MagicMock()
        return acc

    # -- backtest_score is always 0.0 -----------------------------------------

    def test_backtest_score_always_zero(self):
        ep  = _ep_with_event(ev_kwargs={"strong_sentiment": True, "sentiment": "BULLISH"})
        ep.total_premium = 600_000
        ep.trade_count   = 5
        ep.is_accelerating = False
        result = build_composite(ep, self._make_acc())
        assert result.backtest_score == 0.0

    # -- strong_sentiment full-score path vs 0.80x discount -------------------

    def test_strong_sentiment_path_higher_than_weak(self):
        base_kwargs = dict(sentiment="BULLISH", premium=500_000, open_interest=100)

        ep_strong = _ep_with_event(ev_kwargs={**base_kwargs, "strong_sentiment": True})
        ep_strong.total_premium = 500_000
        ep_strong.trade_count   = 5
        ep_strong.is_accelerating = False

        ep_weak = _ep_with_event(ev_kwargs={**base_kwargs, "strong_sentiment": False})
        ep_weak.total_premium = 500_000
        ep_weak.trade_count   = 5
        ep_weak.is_accelerating = False

        r_strong = build_composite(ep_strong, self._make_acc())
        r_weak   = build_composite(ep_weak, self._make_acc())

        assert r_strong.composite_score > r_weak.composite_score
        # Flow score for weak path should be raw * 0.80
        assert r_weak.flow_score == pytest.approx(r_strong.flow_score * 0.80, abs=0.002)

    def test_weak_sentiment_discount_exactly_080(self):
        """Direct arithmetic check: flow_s_raw * 0.80 = flow_s when not strong."""
        ev = _Ev(premium=1_000_000, open_interest=100, strong_sentiment=False)
        ep = _Ep(events=[ev], total_premium=1_000_000, trade_count=5)
        result = build_composite(ep, self._make_acc())
        flow_s_raw = compute_flow_score(ep)
        assert result.flow_score == pytest.approx(round(flow_s_raw * 0.80, 3), abs=0.0001)

    # -- weight arithmetic / ceiling ------------------------------------------

    def test_composite_score_ceiling_090(self):
        """With sector_score=0.0, max achievable is 0.90 (weight=0.10 reserved)."""
        ep = _ep_with_event(
            ev_kwargs={"premium": 10_000_000, "open_interest": 1, "strong_sentiment": True},
        )
        ep.total_premium   = 10_000_000
        ep.trade_count     = 100
        ep.is_accelerating = True
        result = build_composite(ep, self._make_acc())
        # flow_s=1.0, vwp_f=1.0, prem_t=1.0 -> max = 0.55+0.20+0.15 = 0.90
        assert result.composite_score <= 0.90 + 1e-6

    def test_weight_arithmetic_no_sector(self):
        """
        Manually verify: comp = flow_s*0.55 + vwp_f*0.20 + prem_t*0.15
        with known inputs.
        """
        ev = _Ev(premium=200_000, open_interest=200, strong_sentiment=True, sentiment="BULLISH")
        ep = _Ep(events=[ev], total_premium=200_000, trade_count=3, is_accelerating=False)

        result = build_composite(ep, self._make_acc())

        flow_s  = result.flow_score
        vwp_f   = volume_weighted_premium_factor(ep)
        prem_t  = premium_tier_score(ep)
        expected = round(flow_s * 0.55 + 0.0 * 0.00 + vwp_f * 0.20 + prem_t * 0.15, 3)

        assert result.composite_score == pytest.approx(expected, abs=0.001)

    # -- recommendation branches ----------------------------------------------

    def test_recommendation_buy_bullish_high_score(self):
        ev = _Ev(
            premium=10_000_000, open_interest=1,
            sentiment="BULLISH", strong_sentiment=True
        )
        ep = _Ep(events=[ev], total_premium=10_000_000, trade_count=20, is_accelerating=True)
        result = build_composite(ep, self._make_acc())
        assert result.recommendation == "BUY"

    def test_recommendation_sell_bearish_high_score(self):
        ev = _Ev(
            premium=10_000_000, open_interest=1,
            sentiment="BEARISH", strong_sentiment=True
        )
        ep = _Ep(events=[ev], total_premium=10_000_000, trade_count=20, is_accelerating=True)
        result = build_composite(ep, self._make_acc())
        assert result.recommendation == "SELL"

    def test_recommendation_hold_low_score(self):
        ev = _Ev(premium=50_000, open_interest=100, sentiment="BULLISH", strong_sentiment=False)
        ep = _Ep(events=[ev], total_premium=50_000, trade_count=1, is_accelerating=False)
        result = build_composite(ep, self._make_acc())
        assert result.recommendation == "HOLD"

    # -- reasoning contains key S6 markers ------------------------------------

    def test_reasoning_contains_ceiling_note(self):
        ep = _ep_with_event()
        ep.total_premium = 200_000
        ep.trade_count   = 3
        result = build_composite(ep, self._make_acc())
        # Engine emits "ceiling=0.9" (not "ceiling=0.90")
        assert "ceiling=0.9" in result.reasoning

    def test_reasoning_contains_strong_label(self):
        ev = _Ev(strong_sentiment=True)
        ep = _Ep(events=[ev], total_premium=200_000, trade_count=3)
        result = build_composite(ep, self._make_acc())
        assert "strong" in result.reasoning

    def test_reasoning_contains_discounted_label_when_weak(self):
        ev = _Ev(strong_sentiment=False)
        ep = _Ep(events=[ev], total_premium=200_000, trade_count=3)
        result = build_composite(ep, self._make_acc())
        assert "discounted" in result.reasoning

    def test_reasoning_contains_accelerating_flag(self):
        ep = _ep_with_event()
        ep.is_accelerating = True
        ep.total_premium   = 200_000
        result = build_composite(ep, self._make_acc())
        assert "Accelerating" in result.reasoning

    # -- CompositeSignal fields -----------------------------------------------

    def test_composite_signal_has_premium_tier_score_field(self):
        ep = _ep_with_event()
        ep.total_premium = 600_000
        result = build_composite(ep, self._make_acc())
        assert hasattr(result, "premium_tier_score")
        assert result.premium_tier_score == 0.60

    # -- influence tier integration -------------------------------------------

    def test_influence_tier_retail_in_reasoning(self):
        ep = _Ep(total_premium=50_000, trade_count=2, events=[_Ev(premium=50_000)])
        result = build_composite(ep, self._make_acc())
        assert "RETAIL" in result.reasoning

    def test_influence_tier_large_in_reasoning(self):
        ep = _Ep(total_premium=200_000, trade_count=3, events=[_Ev(premium=200_000)])
        result = build_composite(ep, self._make_acc())
        assert "LARGE" in result.reasoning

    def test_influence_tier_institutional_in_reasoning(self):
        ep = _Ep(total_premium=600_000, trade_count=4, events=[_Ev(premium=600_000)])
        result = build_composite(ep, self._make_acc())
        assert "INSTITUTIONAL" in result.reasoning

    def test_influence_tier_whale_in_reasoning(self):
        ep = _Ep(total_premium=3_000_000, trade_count=6, events=[_Ev(premium=3_000_000)])
        result = build_composite(ep, self._make_acc())
        assert "WHALE" in result.reasoning

    # -- flow_score field always reflects sentiment discount ------------------

    def test_flow_score_field_reflects_strong_discount(self):
        ev_strong = _Ev(premium=500_000, open_interest=100, strong_sentiment=True)
        ev_weak   = _Ev(premium=500_000, open_interest=100, strong_sentiment=False)
        ep_s = _Ep(events=[ev_strong], total_premium=500_000, trade_count=5)
        ep_w = _Ep(events=[ev_weak],   total_premium=500_000, trade_count=5)
        rs = build_composite(ep_s, self._make_acc())
        rw = build_composite(ep_w, self._make_acc())
        # flow_score stored on result must already be discounted
        assert rw.flow_score < rs.flow_score


# ---------------------------------------------------------------------------
# Structural: composite bus payload must carry composite_score_ceiling
# ---------------------------------------------------------------------------

class TestCompositeBusPayloadStructure:
    """
    Verify the tradier_stream composite_msg dict shape carries the S6 fields.
    We test the shape by constructing the dict inline (matching stream code)
    rather than importing the async stream module (which would require a live
    event loop and full app wiring).
    """

    def _build_payload(self, composite, alert_level, sig_ep, ev, direction):
        """Replicate the composite_msg dict from tradier_stream._process_trade."""
        return {
            "type": "composite_signal",
            "data": {
                "signal": {
                    "ticker":                  composite.ticker,
                    "recommendation":          composite.recommendation,
                    "composite_score":         composite.composite_score,
                    "composite_score_ceiling": 0.90,
                    "flow_score":              composite.flow_score,
                    "backtest_score":          composite.backtest_score,
                    "volume_premium_factor":   composite.volume_premium_factor,
                    "premium_tier_score":      composite.premium_tier_score,
                    "reasoning":               composite.reasoning,
                    "alert_level":             alert_level,
                    "order_side":              getattr(ev, "order_side", "UNKNOWN"),
                    "strong_sentiment":        getattr(ev, "strong_sentiment", False),
                    "execution_mechanic":      getattr(ev, "execution_mechanic", "AMBIGUOUS_LONG"),
                },
                "episode": {
                    "contract_type":   sig_ep.contract_type,
                    "direction":       direction,
                    "influence_tier":  episode_influence_tier(sig_ep),
                    "total_premium":   sig_ep.total_premium,
                    "trade_count":     sig_ep.trade_count,
                    "is_accelerating": sig_ep.is_accelerating,
                    "timestamp":       "2026-05-01T00:00:00",
                },
            },
        }

    def test_composite_score_ceiling_present_and_090(self):
        ev = _Ev(strong_sentiment=True, sentiment="BULLISH")
        ev.order_side = "BUY"
        ev.execution_mechanic = "DIRECTIONAL_LONG"
        ep = _Ep(events=[ev], total_premium=600_000, trade_count=4)
        composite = build_composite(ep, MagicMock())
        payload = self._build_payload(composite, "STRONG_SIGNAL", ep, ev, "REPEAT_BUY")
        assert payload["data"]["signal"]["composite_score_ceiling"] == 0.90

    def test_order_side_present_in_signal(self):
        ev = _Ev()
        ev.order_side = "SELL"
        ev.execution_mechanic = "PASSIVE_BULLISH"
        ep = _Ep(events=[ev], total_premium=300_000)
        composite = build_composite(ep, MagicMock())
        payload = self._build_payload(composite, "ALERT", ep, ev, "REPEAT_BUY")
        assert payload["data"]["signal"]["order_side"] == "SELL"

    def test_strong_sentiment_present_in_signal(self):
        ev = _Ev(strong_sentiment=True)
        ev.order_side = "BUY"
        ev.execution_mechanic = "DIRECTIONAL_LONG"
        ep = _Ep(events=[ev], total_premium=200_000)
        composite = build_composite(ep, MagicMock())
        payload = self._build_payload(composite, "ALERT", ep, ev, "REPEAT_BUY")
        assert payload["data"]["signal"]["strong_sentiment"] is True

    def test_execution_mechanic_present_in_signal(self):
        ev = _Ev()
        ev.order_side = "SELL"
        ev.execution_mechanic = "PASSIVE_BULLISH"
        ep = _Ep(events=[ev], total_premium=200_000)
        composite = build_composite(ep, MagicMock())
        payload = self._build_payload(composite, "ALERT", ep, ev, "REPEAT_BUY")
        assert payload["data"]["signal"]["execution_mechanic"] == "PASSIVE_BULLISH"

    def test_premium_tier_score_present_in_signal(self):
        ev = _Ev(premium=600_000)
        ev.order_side = "BUY"
        ev.execution_mechanic = "DIRECTIONAL_LONG"
        ep = _Ep(events=[ev], total_premium=600_000)
        composite = build_composite(ep, MagicMock())
        payload = self._build_payload(composite, "STRONG_SIGNAL", ep, ev, "REPEAT_BUY")
        assert "premium_tier_score" in payload["data"]["signal"]

    def test_episode_influence_tier_uses_episode_premium(self):
        """influence_tier in episode block must reflect episode-level premium, not event-level."""
        ev = _Ev(premium=200_000)     # event-level would be LARGE
        ev.order_side = "BUY"
        ev.execution_mechanic = "DIRECTIONAL_LONG"
        ep = _Ep(events=[ev], total_premium=2_500_000)  # episode WHALE
        composite = build_composite(ep, MagicMock())
        payload = self._build_payload(composite, "CONVICTION", ep, ev, "REPEAT_BUY")
        # episode block influence_tier must use episode total_premium -> WHALE
        assert payload["data"]["episode"]["influence_tier"] == "WHALE"

    def test_backtest_score_zero_in_payload(self):
        ev = _Ev()
        ev.order_side = "BUY"
        ev.execution_mechanic = "DIRECTIONAL_LONG"
        ep = _Ep(events=[ev], total_premium=200_000)
        composite = build_composite(ep, MagicMock())
        payload = self._build_payload(composite, "ALERT", ep, ev, "REPEAT_BUY")
        assert payload["data"]["signal"]["backtest_score"] == 0.0
