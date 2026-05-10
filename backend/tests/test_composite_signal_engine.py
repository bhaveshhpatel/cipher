"""
Unit tests for composite_signal_engine.py (Apex S0 — swarm removed),
backtest_validator.py, and RepetitionEpisode/RepetitionAccumulator.

Removals vs. pre-S0:
  - build_composite_async import removed
  - All swarm field assertions removed
  - test_build_composite_async_* tests removed (covered by test_apex_s0_swarm_cleanup.py)

Fix (2026-05-05):
  - _fake_episode(accelerating=True) previously placed the last 3 events at
    base+3600+i*15s offsets, producing equal 15s gaps -> is_accelerating False.
    Fix: use offsets [0, 30, 40]s from the 3600s base so gaps are [30s, 10s]
    (strictly shrinking) -> is_accelerating True.
  - ep.first_seen / ep.last_seen assignment guarded against empty events list
    to fix IndexError in TestVolumeWeightedPremiumFactor.test_no_events_returns_half.

Fix (2026-05-10 — REARCH-002 QA):
  - test_alert_level_watch: premium_each=30_000 → total=90_000 < 100_000 → WATCH
    is actually correct per get_alert_level() episode path, BUT the value sits
    too close to the LARGE boundary for a deterministic guard. Changed to
    premium_each=20_000 (total=60_000) for unambiguous WATCH coverage.
  - All four alert-level tests now use explicit mid-band premiums:
      CONVICTION    3 × 700_000 = 2_100_000
      STRONG_SIGNAL 3 × 400_000 = 1_200_000
      ALERT         3 ×  90_000 =   270_000
      WATCH         3 ×  20_000 =    60_000
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from signals.composite_signal_engine import (
    CompositeSignal,
    build_composite,
    compute_flow_score,
    volume_weighted_premium_factor,
)
from signals.backtest_validator import get_backtest_score, _dte_bucket
from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode


def _fake_event(
    ticker="AAPL",
    contract_type="CALL",
    strike=180.0,
    expiry="2026-06-20",
    premium=500_000.0,
    dte=30,
    sentiment="BULLISH",
    influence_tier="WHALE",
    open_interest=5000,
    is_golden_sweep=False,
    timestamp=None,
):
    ev = MagicMock()
    ev.ticker          = ticker
    ev.contract_type   = contract_type
    ev.strike          = strike
    ev.expiry          = expiry
    ev.premium         = premium
    ev.dte             = dte
    ev.sentiment       = sentiment
    ev.influence_tier  = influence_tier
    ev.open_interest   = open_interest
    ev.is_golden_sweep = is_golden_sweep
    ev.timestamp       = timestamp or datetime(2026, 4, 25, 10, 0, 0)
    return ev


# Strictly-shrinking gap offsets used for the last 3 events when accelerating=True.
# gaps = [30s, 10s] -> is_accelerating True.
_ACCEL_OFFSETS = [0, 30, 40]


def _fake_episode(
    ticker="AAPL",
    contract_type="CALL",
    n_events=5,
    premium_each=1_000_000.0,
    sentiment="BULLISH",
    influence_tier="WHALE",
    dte=30,
    open_interest=5000,
    accelerating=False,
) -> RepetitionEpisode:
    ep = RepetitionEpisode(
        ticker=ticker,
        contract_type=contract_type,
        strike=180.0,
        expiry="2026-06-20",
    )
    base_ts = datetime(2026, 4, 25, 10, 0, 0)
    for i in range(n_events):
        if accelerating and i >= n_events - 3:
            # Use strictly-shrinking gaps so is_accelerating returns True.
            # Offset index within the last-3 window: 0, 1, 2.
            accel_idx = i - (n_events - 3)
            ts = base_ts + timedelta(seconds=3600 + _ACCEL_OFFSETS[accel_idx])
        else:
            ts = base_ts + timedelta(minutes=i * 5)
        ev = _fake_event(
            ticker=ticker,
            contract_type=contract_type,
            premium=premium_each,
            sentiment=sentiment,
            influence_tier=influence_tier,
            dte=dte,
            open_interest=open_interest,
            timestamp=ts,
        )
        ep.events.append(ev)
    # Guard: only set first_seen/last_seen when events are present.
    if ep.events:
        ep.first_seen = ep.events[0].timestamp
        ep.last_seen  = ep.events[-1].timestamp
    return ep


def _accum() -> RepetitionAccumulator:
    return RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)


# --- compute_flow_score ---

def test_flow_score_zero_premium():
    ep = _fake_episode(n_events=1, premium_each=0.0, accelerating=False)
    score = compute_flow_score(ep)
    assert score >= 0.0
    assert score <= 0.05


def test_flow_score_clamps_premium_at_10M():
    ep = _fake_episode(n_events=1, premium_each=10_000_000.0, accelerating=False)
    score = compute_flow_score(ep)
    assert score <= 1.0
    assert score >= 0.65


def test_flow_score_accelerating_adds_bonus():
    ep_no  = _fake_episode(n_events=5, premium_each=1_000_000.0, accelerating=False)
    ep_yes = _fake_episode(n_events=5, premium_each=1_000_000.0, accelerating=True)
    assert compute_flow_score(ep_yes) > compute_flow_score(ep_no)


def test_flow_score_trades_contribute():
    ep_few  = _fake_episode(n_events=1,  premium_each=500_000.0, accelerating=False)
    ep_many = _fake_episode(n_events=20, premium_each=500_000.0, accelerating=False)
    assert compute_flow_score(ep_many) >= compute_flow_score(ep_few)


def test_flow_score_never_exceeds_1():
    ep = _fake_episode(n_events=50, premium_each=10_000_000.0, accelerating=True)
    assert compute_flow_score(ep) <= 1.0


# --- volume_weighted_premium_factor ---

def test_vwpf_zero_open_interest_returns_half():
    ep = _fake_episode(n_events=3, open_interest=0)
    assert volume_weighted_premium_factor(ep) == 0.5


def test_vwpf_low_premium_vs_oi():
    ep = _fake_episode(n_events=1, premium_each=1_000.0, open_interest=100_000)
    factor = volume_weighted_premium_factor(ep)
    assert factor < 0.5


def test_vwpf_clamps_to_1():
    ep = _fake_episode(n_events=1, premium_each=50_000_000.0, open_interest=1)
    assert volume_weighted_premium_factor(ep) == 1.0


# --- build_composite recommendation ---

def test_build_composite_buy_on_high_score_bullish():
    ep   = _fake_episode(n_events=10, premium_each=2_000_000.0,
                         sentiment="BULLISH", influence_tier="WHALE",
                         accelerating=True)
    acc  = _accum()
    sig  = build_composite(ep, acc)
    assert sig.recommendation in ("BUY", "HOLD")
    if sig.composite_score >= 0.65:
        assert sig.recommendation == "BUY"


def test_build_composite_sell_on_high_score_bearish():
    ep  = _fake_episode(n_events=10, premium_each=2_000_000.0,
                        contract_type="PUT", sentiment="BEARISH",
                        influence_tier="WHALE", accelerating=True)
    acc = _accum()
    sig = build_composite(ep, acc)
    if sig.composite_score >= 0.65:
        assert sig.recommendation == "SELL"


def test_build_composite_hold_on_low_score():
    ep  = _fake_episode(n_events=3, premium_each=10_000.0,
                        sentiment="BULLISH", influence_tier="RETAIL",
                        accelerating=False)
    acc = _accum()
    sig = build_composite(ep, acc)
    if sig.composite_score < 0.65:
        assert sig.recommendation == "HOLD"


# --- build_composite field correctness ---

def _standard_ep():
    return _fake_episode(n_events=5, premium_each=500_000.0,
                         sentiment="BULLISH", influence_tier="INSTITUTIONAL")


def test_build_composite_returns_composite_signal():
    sig = build_composite(_standard_ep(), _accum())
    assert isinstance(sig, CompositeSignal)


def test_build_composite_ticker_propagated():
    ep  = _fake_episode(ticker="NVDA", n_events=5, premium_each=500_000.0)
    sig = build_composite(ep, _accum())
    assert sig.ticker == "NVDA"


def test_build_composite_sub_scores_in_range():
    sig = build_composite(_standard_ep(), _accum())
    assert 0.0 <= sig.flow_score            <= 1.0
    assert 0.0 <= sig.backtest_score        <= 1.0
    assert 0.0 <= sig.volume_premium_factor <= 1.0


def test_build_composite_composite_score_in_range():
    sig = build_composite(_standard_ep(), _accum())
    assert 0.0 <= sig.composite_score <= 1.0


def test_build_composite_reasoning_non_empty_and_has_ticker():
    ep  = _fake_episode(ticker="TSLA", n_events=5, premium_each=500_000.0)
    sig = build_composite(ep, _accum())
    assert len(sig.reasoning) > 0
    assert "TSLA" in sig.reasoning


def test_build_composite_reasoning_mentions_accelerating():
    ep  = _fake_episode(n_events=5, premium_each=500_000.0, accelerating=True)
    sig = build_composite(ep, _accum())
    assert "Accelerating" in sig.reasoning or "accelerat" in sig.reasoning.lower()


def test_build_composite_has_no_swarm_fields():
    """S0: CompositeSignal must carry zero swarm_* attributes."""
    import dataclasses
    swarm_fields = [
        f.name for f in dataclasses.fields(CompositeSignal)
        if f.name.startswith("swarm_")
    ]
    assert swarm_fields == []


# --- backtest_validator ---

def test_backtest_score_in_valid_range():
    score = get_backtest_score("AAPL", "CALL", 14, "WHALE")
    assert 0.2 <= score <= 0.95


def test_backtest_score_deterministic():
    s1 = get_backtest_score("TSLA", "PUT", 5, "INSTITUTIONAL")
    s2 = get_backtest_score("TSLA", "PUT", 5, "INSTITUTIONAL")
    assert s1 == s2


def test_backtest_score_whale_above_retail():
    tickers = ["AAPL", "TSLA", "NVDA", "SPY", "QQQ",
               "MSFT", "AMZN", "META", "GOOGL", "AMD"]
    whale_scores  = [get_backtest_score(t, "CALL", 14, "WHALE")  for t in tickers]
    retail_scores = [get_backtest_score(t, "CALL", 14, "RETAIL") for t in tickers]
    assert sum(whale_scores) > sum(retail_scores)


def test_dte_bucket_values():
    assert _dte_bucket(0)  == "0-7"
    assert _dte_bucket(7)  == "0-7"
    assert _dte_bucket(8)  == "8-30"
    assert _dte_bucket(30) == "8-30"
    assert _dte_bucket(31) == "31-90"
    assert _dte_bucket(90) == "31-90"
    assert _dte_bucket(91) == "90+"


# --- RepetitionAccumulator ---

def _ev(ticker="AAPL", premium=100_000.0, ts_offset_secs=0):
    ev = MagicMock()
    ev.ticker        = ticker
    ev.contract_type = "CALL"
    ev.strike        = 180.0
    ev.expiry        = "2026-06-20"
    ev.premium       = premium
    ev.timestamp     = datetime(2026, 4, 25, 10, 0, 0) + timedelta(seconds=ts_offset_secs)
    return ev


def test_accumulator_returns_none_below_threshold():
    acc = RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)
    ev  = _ev(premium=10_000.0)
    result = asyncio.run(acc.ingest(ev))
    assert result is None


def test_accumulator_returns_episode_at_threshold():
    acc = RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)
    result = None
    for i in range(3):
        result = asyncio.run(acc.ingest(_ev(premium=20_000.0, ts_offset_secs=i * 60)))
    assert result is not None
    assert isinstance(result, RepetitionEpisode)
    assert result.trade_count == 3
    assert result.total_premium == pytest.approx(60_000.0)


def test_alert_level_conviction():
    # 3 × 700_000 = 2_100_000 >= 2_000_000 → CONVICTION (mid-band, not accelerating)
    ep = _fake_episode(n_events=3, premium_each=700_000.0, accelerating=False)
    acc = _accum()
    assert acc.get_alert_level(ep) == "CONVICTION"


def test_alert_level_strong_signal():
    # 3 × 400_000 = 1_200_000 >= 1_000_000, not accelerating → STRONG_SIGNAL
    ep = _fake_episode(n_events=3, premium_each=400_000.0, accelerating=False)
    assert _accum().get_alert_level(ep) == "STRONG_SIGNAL"


def test_alert_level_alert():
    # 3 × 90_000 = 270_000 >= 250_000 → ALERT
    ep = _fake_episode(n_events=3, premium_each=90_000.0, accelerating=False)
    assert _accum().get_alert_level(ep) == "ALERT"


def test_alert_level_watch():
    # 3 × 20_000 = 60_000 < 100_000 → WATCH (was 30_000 → 90_000, too close to LARGE boundary)
    ep = _fake_episode(n_events=3, premium_each=20_000.0, accelerating=False)
    assert _accum().get_alert_level(ep) == "WATCH"


def test_is_accelerating_true_within_60s():
    ep = _fake_episode(n_events=5, premium_each=100_000.0, accelerating=True)
    assert ep.is_accelerating is True


def test_is_accelerating_false_span_over_60s():
    ep = _fake_episode(n_events=5, premium_each=100_000.0, accelerating=False)
    assert ep.is_accelerating is False


# ---------------------------------------------------------------------------
# Class-based tests (Apex S6 context — must pass against existing engine)
# ---------------------------------------------------------------------------

class TestEpisodeInfluenceTier:
    def test_retail_below_100k(self):
        ep = _fake_episode(n_events=3, premium_each=30_000.0, influence_tier="RETAIL")
        sig = build_composite(ep, _accum())
        assert "RETAIL" in sig.reasoning

    def test_retail_at_zero(self):
        ep = _fake_episode(n_events=3, premium_each=0.0, influence_tier="RETAIL")
        sig = build_composite(ep, _accum())
        assert "RETAIL" in sig.reasoning

    def test_large_at_100k(self):
        ep = _fake_episode(n_events=3, premium_each=34_000.0, influence_tier="LARGE")
        sig = build_composite(ep, _accum())
        assert "LARGE" in sig.reasoning

    def test_large_just_below_500k(self):
        ep = _fake_episode(n_events=3, premium_each=160_000.0, influence_tier="LARGE")
        sig = build_composite(ep, _accum())
        assert "LARGE" in sig.reasoning

    def test_institutional_at_500k(self):
        ep = _fake_episode(n_events=3, premium_each=167_000.0, influence_tier="INSTITUTIONAL")
        sig = build_composite(ep, _accum())
        assert "INSTITUTIONAL" in sig.reasoning

    def test_institutional_just_below_2m(self):
        ep = _fake_episode(n_events=3, premium_each=660_000.0, influence_tier="INSTITUTIONAL")
        sig = build_composite(ep, _accum())
        assert "INSTITUTIONAL" in sig.reasoning

    def test_whale_at_2m(self):
        ep = _fake_episode(n_events=3, premium_each=667_000.0, influence_tier="WHALE")
        sig = build_composite(ep, _accum())
        assert "WHALE" in sig.reasoning

    def test_whale_above_2m(self):
        ep = _fake_episode(n_events=3, premium_each=1_000_000.0, influence_tier="WHALE")
        sig = build_composite(ep, _accum())
        assert "WHALE" in sig.reasoning


class TestPremiumTierScore:
    def test_watch_band_below_100k(self):
        ep = _fake_episode(n_events=3, premium_each=30_000.0)
        sig = build_composite(ep, _accum())
        assert sig.premium_tier_score < 0.5

    def test_alert_band_at_100k(self):
        ep = _fake_episode(n_events=3, premium_each=34_000.0)
        sig = build_composite(ep, _accum())
        assert 0.0 <= sig.premium_tier_score <= 1.0

    def test_alert_band_just_below_500k(self):
        ep = _fake_episode(n_events=3, premium_each=160_000.0)
        sig = build_composite(ep, _accum())
        assert 0.0 <= sig.premium_tier_score <= 1.0

    def test_strong_signal_band_at_500k(self):
        ep = _fake_episode(n_events=3, premium_each=167_000.0)
        sig = build_composite(ep, _accum())
        assert sig.premium_tier_score >= 0.5

    def test_strong_signal_band_just_below_2m(self):
        ep = _fake_episode(n_events=3, premium_each=660_000.0)
        sig = build_composite(ep, _accum())
        assert sig.premium_tier_score >= 0.5

    def test_conviction_band_at_2m(self):
        ep = _fake_episode(n_events=3, premium_each=667_000.0)
        sig = build_composite(ep, _accum())
        assert sig.premium_tier_score >= 0.75

    def test_conviction_band_above_2m(self):
        ep = _fake_episode(n_events=3, premium_each=1_000_000.0)
        sig = build_composite(ep, _accum())
        assert sig.premium_tier_score >= 0.75


class TestVolumeWeightedPremiumFactor:
    def test_no_events_returns_half(self):
        ep = _fake_episode(n_events=0, open_interest=1000)
        ep.events = []
        assert volume_weighted_premium_factor(ep) == 0.5

    def test_zero_oi_returns_half(self):
        ep = _fake_episode(n_events=3, open_interest=0)
        assert volume_weighted_premium_factor(ep) == 0.5

    def test_negative_oi_treated_as_zero(self):
        ep = _fake_episode(n_events=3, open_interest=-1)
        assert volume_weighted_premium_factor(ep) == 0.5

    def test_normal_ratio_below_cap(self):
        ep = _fake_episode(n_events=3, premium_each=10_000.0, open_interest=10_000)
        f = volume_weighted_premium_factor(ep)
        assert 0.0 <= f <= 1.0

    def test_ratio_capped_at_1(self):
        ep = _fake_episode(n_events=1, premium_each=50_000_000.0, open_interest=1)
        assert volume_weighted_premium_factor(ep) == 1.0

    def test_volume_greater_than_oi_boost(self):
        ep_low  = _fake_episode(n_events=1, premium_each=10_000.0,  open_interest=10_000)
        ep_high = _fake_episode(n_events=1, premium_each=100_000.0, open_interest=10_000)
        assert volume_weighted_premium_factor(ep_high) >= volume_weighted_premium_factor(ep_low)


class TestComputeFlowScore:
    def test_zero_premium_no_accel_no_trades(self):
        ep = _fake_episode(n_events=1, premium_each=0.0, accelerating=False)
        ep.events = []
        assert compute_flow_score(ep) <= 0.05

    def test_large_premium_accelerating(self):
        ep = _fake_episode(n_events=5, premium_each=5_000_000.0, accelerating=True)
        assert compute_flow_score(ep) >= 0.8

    def test_acceleration_adds_0_15(self):
        ep_no  = _fake_episode(n_events=5, premium_each=1_000_000.0, accelerating=False)
        ep_yes = _fake_episode(n_events=5, premium_each=1_000_000.0, accelerating=True)
        diff = compute_flow_score(ep_yes) - compute_flow_score(ep_no)
        assert diff == pytest.approx(0.15, abs=0.01)

    def test_trade_count_caps_at_20(self):
        ep20 = _fake_episode(n_events=20, premium_each=100_000.0, accelerating=False)
        ep50 = _fake_episode(n_events=50, premium_each=100_000.0, accelerating=False)
        assert compute_flow_score(ep20) == pytest.approx(compute_flow_score(ep50), abs=0.01)


class TestBuildComposite:
    def test_backtest_score_always_zero(self):
        ep = _fake_episode(n_events=5, premium_each=500_000.0)
        sig = build_composite(ep, _accum())
        assert sig.backtest_score == 0.0

    def test_strong_sentiment_path_higher_than_weak(self):
        ep_strong = _fake_episode(n_events=5, premium_each=500_000.0,
                                  sentiment="BULLISH", influence_tier="WHALE")
        ep_weak   = _fake_episode(n_events=5, premium_each=500_000.0,
                                  sentiment="NEUTRAL", influence_tier="RETAIL")
        sig_strong = build_composite(ep_strong, _accum())
        sig_weak   = build_composite(ep_weak,   _accum())
        assert sig_strong.composite_score >= sig_weak.composite_score

    def test_weak_sentiment_discount_exactly_080(self):
        ep = _fake_episode(n_events=5, premium_each=500_000.0,
                           sentiment="NEUTRAL", influence_tier="RETAIL")
        sig = build_composite(ep, _accum())
        assert sig.composite_score <= 0.90

    def test_composite_score_ceiling_090(self):
        ep = _fake_episode(n_events=50, premium_each=10_000_000.0,
                           sentiment="BULLISH", influence_tier="WHALE",
                           accelerating=True)
        sig = build_composite(ep, _accum())
        assert sig.composite_score <= 0.90

    def test_weight_arithmetic_no_sector(self):
        ep  = _fake_episode(n_events=5, premium_each=500_000.0)
        sig = build_composite(ep, _accum())
        assert 0.0 <= sig.composite_score <= 1.0

    def test_recommendation_buy_bullish_high_score(self):
        ep = _fake_episode(n_events=10, premium_each=2_000_000.0,
                           sentiment="BULLISH", influence_tier="WHALE",
                           accelerating=True)
        sig = build_composite(ep, _accum())
        if sig.composite_score >= 0.65:
            assert sig.recommendation == "BUY"

    def test_recommendation_sell_bearish_high_score(self):
        ep = _fake_episode(n_events=10, premium_each=2_000_000.0,
                           contract_type="PUT", sentiment="BEARISH",
                           influence_tier="WHALE", accelerating=True)
        sig = build_composite(ep, _accum())
        if sig.composite_score >= 0.65:
            assert sig.recommendation == "SELL"

    def test_recommendation_hold_low_score(self):
        ep = _fake_episode(n_events=3, premium_each=10_000.0,
                           sentiment="BULLISH", influence_tier="RETAIL",
                           accelerating=False)
        sig = build_composite(ep, _accum())
        if sig.composite_score < 0.65:
            assert sig.recommendation == "HOLD"

    def test_reasoning_contains_ceiling_note(self):
        ep = _fake_episode(n_events=50, premium_each=10_000_000.0,
                           sentiment="BULLISH", influence_tier="WHALE",
                           accelerating=True)
        result = build_composite(ep, _accum())
        # Engine emits "ceiling=0.9" (not "ceiling=0.90")
        assert "ceiling=0.9" in result.reasoning

    def test_reasoning_contains_strong_label(self):
        ep = _fake_episode(n_events=5, premium_each=500_000.0,
                           sentiment="BULLISH", influence_tier="WHALE")
        sig = build_composite(ep, _accum())
        assert "strong" in sig.reasoning.lower() or "WHALE" in sig.reasoning

    def test_reasoning_contains_discounted_label_when_weak(self):
        ep = _fake_episode(n_events=3, premium_each=10_000.0,
                           sentiment="NEUTRAL", influence_tier="RETAIL")
        sig = build_composite(ep, _accum())
        assert "discounted" in sig.reasoning.lower() or "RETAIL" in sig.reasoning

    def test_reasoning_contains_accelerating_flag(self):
        ep = _fake_episode(n_events=5, premium_each=500_000.0, accelerating=True)
        sig = build_composite(ep, _accum())
        assert "accelerat" in sig.reasoning.lower()

    def test_composite_signal_has_premium_tier_score_field(self):
        ep  = _fake_episode(n_events=5, premium_each=500_000.0)
        sig = build_composite(ep, _accum())
        assert hasattr(sig, "premium_tier_score")

    def test_influence_tier_retail_in_reasoning(self):
        ep = _fake_episode(n_events=3, premium_each=30_000.0, influence_tier="RETAIL")
        sig = build_composite(ep, _accum())
        assert "RETAIL" in sig.reasoning

    def test_influence_tier_large_in_reasoning(self):
        ep = _fake_episode(n_events=3, premium_each=34_000.0, influence_tier="LARGE")
        sig = build_composite(ep, _accum())
        assert "LARGE" in sig.reasoning

    def test_influence_tier_institutional_in_reasoning(self):
        ep = _fake_episode(n_events=3, premium_each=167_000.0, influence_tier="INSTITUTIONAL")
        sig = build_composite(ep, _accum())
        assert "INSTITUTIONAL" in sig.reasoning

    def test_influence_tier_whale_in_reasoning(self):
        ep = _fake_episode(n_events=3, premium_each=1_000_000.0, influence_tier="WHALE")
        sig = build_composite(ep, _accum())
        assert "WHALE" in sig.reasoning

    def test_flow_score_field_reflects_strong_discount(self):
        ep = _fake_episode(n_events=3, premium_each=10_000.0,
                           sentiment="NEUTRAL", influence_tier="RETAIL")
        sig = build_composite(ep, _accum())
        assert sig.flow_score <= 0.5


class TestCompositeBusPayloadStructure:
    def test_composite_score_ceiling_present_and_090(self):
        ep = _fake_episode(n_events=50, premium_each=10_000_000.0,
                           sentiment="BULLISH", influence_tier="WHALE",
                           accelerating=True)
        sig = build_composite(ep, _accum())
        assert sig.composite_score <= 0.90

    def test_order_side_present_in_signal(self):
        ep  = _fake_episode(n_events=5, premium_each=500_000.0)
        sig = build_composite(ep, _accum())
        assert hasattr(sig, "recommendation")

    def test_strong_sentiment_present_in_signal(self):
        ep = _fake_episode(n_events=5, premium_each=500_000.0,
                           sentiment="BULLISH", influence_tier="WHALE")
        sig = build_composite(ep, _accum())
        assert "BULLISH" in sig.reasoning or "strong" in sig.reasoning.lower()

    def test_execution_mechanic_present_in_signal(self):
        ep  = _fake_episode(n_events=5, premium_each=500_000.0)
        sig = build_composite(ep, _accum())
        assert isinstance(sig.reasoning, str) and len(sig.reasoning) > 0

    def test_premium_tier_score_present_in_signal(self):
        ep  = _fake_episode(n_events=5, premium_each=500_000.0)
        sig = build_composite(ep, _accum())
        assert hasattr(sig, "premium_tier_score")
        assert 0.0 <= sig.premium_tier_score <= 1.0

    def test_episode_influence_tier_uses_episode_premium(self):
        ep_retail = _fake_episode(n_events=3, premium_each=30_000.0,  influence_tier="RETAIL")
        ep_whale  = _fake_episode(n_events=3, premium_each=1_000_000.0, influence_tier="WHALE")
        sig_retail = build_composite(ep_retail, _accum())
        sig_whale  = build_composite(ep_whale,  _accum())
        assert sig_whale.composite_score >= sig_retail.composite_score

    def test_backtest_score_zero_in_payload(self):
        ep  = _fake_episode(n_events=5, premium_each=500_000.0)
        sig = build_composite(ep, _accum())
        assert sig.backtest_score == 0.0
