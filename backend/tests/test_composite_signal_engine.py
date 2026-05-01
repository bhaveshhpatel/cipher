"""
Unit tests for composite_signal_engine.py (Apex S0 — swarm removed),
backtest_validator.py, and RepetitionEpisode/RepetitionAccumulator.

Removals vs. pre-S0:
  - build_composite_async import removed
  - All swarm field assertions removed
  - test_build_composite_async_* tests removed (covered by test_apex_s0_swarm_cleanup.py)
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
            ts = base_ts + timedelta(seconds=3600 + i * 15)
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
    ep = _fake_episode(n_events=3, premium_each=2_000_000.0, accelerating=False)
    acc = _accum()
    assert acc.get_alert_level(ep) == "CONVICTION"


def test_alert_level_strong_signal():
    ep = _fake_episode(n_events=3, premium_each=400_000.0, accelerating=False)
    assert _accum().get_alert_level(ep) == "STRONG_SIGNAL"


def test_alert_level_alert():
    ep = _fake_episode(n_events=3, premium_each=90_000.0, accelerating=False)
    assert _accum().get_alert_level(ep) == "ALERT"


def test_alert_level_watch():
    ep = _fake_episode(n_events=3, premium_each=30_000.0, accelerating=False)
    assert _accum().get_alert_level(ep) == "WATCH"


def test_is_accelerating_true_within_60s():
    ep = _fake_episode(n_events=5, premium_each=100_000.0, accelerating=True)
    assert ep.is_accelerating is True


def test_is_accelerating_false_span_over_60s():
    ep = _fake_episode(n_events=5, premium_each=100_000.0, accelerating=False)
    assert ep.is_accelerating is False
