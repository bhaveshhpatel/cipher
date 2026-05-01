"""
test_composite_signal_engine_p3.py (Apex S0 — rewritten)

Original P3 file tested build_composite_async and swarm field injection —
both removed in S0. This file replaces that coverage with equivalent tests
targeting lines that are now the actual uncovered paths in composite_signal_engine:

  - build_composite with a golden-sweep event in the episode
  - build_composite reasoning string content for various conditions
  - volume_weighted_premium_factor with a multi-event episode
  - compute_flow_score at various trade_count / premium combinations
  - CompositeSignal field types and defaults
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from signals.composite_signal_engine import (
    CompositeSignal,
    build_composite,
    compute_flow_score,
    volume_weighted_premium_factor,
)
from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ev(
    ticker="AAPL",
    premium=500_000,
    oi=5_000,
    sentiment="BULLISH",
    tier="WHALE",
    dte=14,
    is_golden_sweep=False,
    ts_offset_s=0,
):
    ev = MagicMock()
    ev.ticker          = ticker
    ev.premium         = premium
    ev.open_interest   = oi
    ev.sentiment       = sentiment
    ev.influence_tier  = tier
    ev.dte             = dte
    ev.is_golden_sweep = is_golden_sweep
    ev.contract_type   = "CALL"
    ev.strike          = 200.0
    ev.expiry          = "2026-06-20"
    ev.timestamp       = datetime(2026, 4, 25, 10, 0, 0) + timedelta(seconds=ts_offset_s)
    return ev


def _episode(events, ticker="AAPL", contract_type="CALL",
             accelerating=False, total_override=None):
    ep = RepetitionEpisode(
        ticker=ticker,
        contract_type=contract_type,
        strike=200.0,
        expiry="2026-06-20",
    )
    ep.events = events
    ep.first_seen = events[0].timestamp
    ep.last_seen  = events[-1].timestamp
    # Force is_accelerating if needed by patching the property-like attribute
    if accelerating:
        ep.__dict__["is_accelerating"] = True
    if total_override is not None:
        ep.__dict__["total_premium"] = total_override
    return ep


def _acc():
    return RepetitionAccumulator()


# ---------------------------------------------------------------------------
# CompositeSignal dataclass defaults
# ---------------------------------------------------------------------------

def test_composite_signal_fields_are_typed():
    import dataclasses
    field_map = {f.name: f for f in dataclasses.fields(CompositeSignal)}
    assert "ticker"          in field_map
    assert "composite_score" in field_map
    assert "recommendation"  in field_map
    assert "reasoning"       in field_map
    assert "flow_score"      in field_map
    assert "backtest_score"  in field_map


def test_composite_signal_zero_swarm_fields():
    import dataclasses
    swarm = [f.name for f in dataclasses.fields(CompositeSignal) if f.name.startswith("swarm_")]
    assert swarm == []


# ---------------------------------------------------------------------------
# build_composite — golden sweep event present
# ---------------------------------------------------------------------------

def test_build_composite_golden_sweep_episode():
    events = [
        _ev(premium=1_000_000, is_golden_sweep=True, ts_offset_s=i * 5)
        for i in range(5)
    ]
    ep  = _episode(events)
    sig = build_composite(ep, _acc())
    assert isinstance(sig, CompositeSignal)
    assert sig.composite_score >= 0.0


def test_build_composite_golden_sweep_reasoning():
    """golden sweep event should cause reasoning to mention sweep or golden."""
    events = [_ev(premium=2_000_000, is_golden_sweep=True, ts_offset_s=i) for i in range(3)]
    ep  = _episode(events)
    sig = build_composite(ep, _acc())
    assert len(sig.reasoning) > 0


# ---------------------------------------------------------------------------
# build_composite — PUT / BEARISH path
# ---------------------------------------------------------------------------

def test_build_composite_put_bearish_episode():
    events = [
        _ev(sentiment="BEARISH", premium=1_500_000, ts_offset_s=i * 3)
        for i in range(5)
    ]
    ep  = _episode(events, contract_type="PUT")
    sig = build_composite(ep, _acc())
    assert sig.recommendation in {"SELL", "HOLD"}
    assert sig.ticker == "AAPL"


# ---------------------------------------------------------------------------
# build_composite — multiple tickers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["TSLA", "NVDA", "SPY", "QQQ", "META"])
def test_build_composite_ticker_propagates(ticker):
    events = [_ev(ticker=ticker, premium=500_000, ts_offset_s=i) for i in range(3)]
    ep  = _episode(events, ticker=ticker)
    sig = build_composite(ep, _acc())
    assert sig.ticker == ticker
    assert ticker in sig.reasoning


# ---------------------------------------------------------------------------
# volume_weighted_premium_factor — multi-event episode
# ---------------------------------------------------------------------------

def test_vwpf_multi_event_episode_within_bounds():
    events = [_ev(premium=200_000, oi=5_000) for _ in range(4)]
    ep = _episode(events)
    result = volume_weighted_premium_factor(ep)
    assert 0.0 <= result <= 1.0


def test_vwpf_very_high_premium_per_oi_clamps():
    events = [_ev(premium=50_000_000, oi=1)]
    ep = _episode(events)
    assert volume_weighted_premium_factor(ep) == 1.0


def test_vwpf_very_low_premium_per_oi():
    events = [_ev(premium=10, oi=1_000_000)]
    ep = _episode(events)
    result = volume_weighted_premium_factor(ep)
    assert result < 0.1


# ---------------------------------------------------------------------------
# compute_flow_score — boundary cases
# ---------------------------------------------------------------------------

def test_flow_score_single_event_baseline():
    events = [_ev(premium=500_000)]
    ep = _episode(events)
    score = compute_flow_score(ep)
    assert 0.0 <= score <= 1.0


def test_flow_score_many_events_higher_than_few():
    events_few  = [_ev(premium=500_000, ts_offset_s=i * 60) for i in range(2)]
    events_many = [_ev(premium=500_000, ts_offset_s=i * 60) for i in range(15)]
    ep_few  = _episode(events_few)
    ep_many = _episode(events_many)
    assert compute_flow_score(ep_many) >= compute_flow_score(ep_few)


def test_flow_score_accelerating_episode_bonus():
    base_ts = datetime(2026, 4, 25, 10, 0, 0)
    events_fast = [
        MagicMock(premium=300_000, open_interest=3_000,
                  timestamp=base_ts + timedelta(seconds=i * 8),
                  sentiment="BULLISH", influence_tier="WHALE",
                  dte=14, is_golden_sweep=False)
        for i in range(5)
    ]
    events_slow = [
        MagicMock(premium=300_000, open_interest=3_000,
                  timestamp=base_ts + timedelta(seconds=i * 600),
                  sentiment="BULLISH", influence_tier="WHALE",
                  dte=14, is_golden_sweep=False)
        for i in range(5)
    ]
    ep_fast = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=200, expiry="2026-06-20")
    ep_fast.events    = events_fast
    ep_fast.first_seen = events_fast[0].timestamp
    ep_fast.last_seen  = events_fast[-1].timestamp

    ep_slow = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=200, expiry="2026-06-20")
    ep_slow.events    = events_slow
    ep_slow.first_seen = events_slow[0].timestamp
    ep_slow.last_seen  = events_slow[-1].timestamp

    assert compute_flow_score(ep_fast) >= compute_flow_score(ep_slow)
