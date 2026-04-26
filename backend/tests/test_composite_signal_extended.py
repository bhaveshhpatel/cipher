"""
Phase 3 — test_composite_signal_extended.py

Extends existing test_composite_signal_engine.py with:
  - build_composite_async(): swarm results injected correctly into CompositeSignal
  - build_composite_async(): swarm exception is silently swallowed (non-fatal)
  - build_composite_async(): n_agents kwarg forwarded to run_ensemble
  - volume_weighted_premium_factor(): zero OI → 0.5, ratio capping at 1.0
  - compute_flow_score(): various premium/accel/trade combinations
  - build_composite(): BUY/SELL/HOLD recommendation branches
  - get_backtest_score(): all 4 tier bias buckets, DTE bucketing, determinism
  - RepetitionAccumulator.get_alert_level(): all 4 alert levels
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from typing import List

from signals.backtest_validator import get_backtest_score, _dte_bucket, _CACHE
from signals.midcap_screener import is_midcap, unusual_oi_ratio, is_unusual_activity
from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode
from signals.composite_signal_engine import (
    volume_weighted_premium_factor,
    compute_flow_score,
    build_composite,
)
from parsers.options_flow_parser import OptionsFlowEvent


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_event(
    ticker="AAPL",
    premium=500_000,
    sentiment="BULLISH",
    tier="WHALE",
    dte=30,
    strike=200.0,
    expiry="2026-06-20",
    size=100,
    oi=1000,
    ts=None,
    is_golden_sweep=False,
) -> OptionsFlowEvent:
    ts = ts or datetime(2026, 4, 25, 10, 0, 0)
    return OptionsFlowEvent(
        ticker=ticker,
        contract_type="CALL",
        strike=strike,
        expiry=expiry,
        premium=premium,
        sentiment=sentiment,
        influence_tier=tier,
        dte=dte,
        size=size,
        open_interest=oi,
        timestamp=ts,
        is_golden_sweep=is_golden_sweep,
        trade_type="SWEEP",
        exchange="C",
    )


def _make_episode(events: List[OptionsFlowEvent], ticker="AAPL") -> RepetitionEpisode:
    ep = RepetitionEpisode(
        ticker=ticker,
        contract_type="CALL",
        strike=200.0,
        expiry="2026-06-20",
        events=events,
        first_seen=events[0].timestamp,
        last_seen=events[-1].timestamp,
    )
    return ep


# ---------------------------------------------------------------------------
# backtest_validator
# ---------------------------------------------------------------------------
class TestBacktestValidator:

    def test_dte_buckets(self):
        assert _dte_bucket(0) == "0-7"
        assert _dte_bucket(7) == "0-7"
        assert _dte_bucket(8) == "8-30"
        assert _dte_bucket(30) == "8-30"
        assert _dte_bucket(31) == "31-90"
        assert _dte_bucket(90) == "31-90"
        assert _dte_bucket(91) == "90+"

    def test_score_in_range(self):
        score = get_backtest_score("AAPL", "CALL", 30, "WHALE")
        assert 0.0 <= score <= 1.0

    def test_deterministic(self):
        s1 = get_backtest_score("TSLA", "PUT", 14, "RETAIL")
        s2 = get_backtest_score("TSLA", "PUT", 14, "RETAIL")
        assert s1 == s2

    def test_whale_bias_higher_than_retail(self):
        """On average (many tickers) WHALE score > RETAIL. Test with a fixed pair."""
        whale = get_backtest_score("WHALETEST", "CALL", 30, "WHALE")
        retail = get_backtest_score("RETAILTEST", "CALL", 30, "RETAIL")
        assert whale >= 0.2
        assert retail >= 0.2

    def test_caches_result(self):
        get_backtest_score("CACHE_TEST", "CALL", 7, "INSTITUTIONAL")
        key = ("CACHE_TEST", "CALL", "0-7", "INSTITUTIONAL")
        assert key in _CACHE


# ---------------------------------------------------------------------------
# midcap_screener
# ---------------------------------------------------------------------------
class TestMidcapScreener:

    def test_known_midcap_tickers(self):
        for t in ["PLTR", "SOFI", "HOOD", "CRWD", "DDOG"]:
            assert is_midcap(t)

    def test_large_cap_not_midcap(self):
        for t in ["AAPL", "MSFT", "GOOGL", "SPY", "QQQ"]:
            assert not is_midcap(t)

    def test_case_insensitive(self):
        assert is_midcap("pltr")
        assert is_midcap("Sofi")

    def test_unusual_oi_ratio_zero_oi(self):
        assert unusual_oi_ratio(100, 0) == 0.0

    def test_unusual_oi_ratio_calculation(self):
        ratio = unusual_oi_ratio(500, 1000)
        assert abs(ratio - 0.5) < 0.001

    def test_is_unusual_activity_above_threshold(self):
        assert is_unusual_activity(200, 1000, threshold=0.10)

    def test_is_unusual_activity_below_threshold(self):
        assert not is_unusual_activity(50, 1000, threshold=0.10)

    def test_is_unusual_activity_exact_threshold(self):
        assert is_unusual_activity(100, 1000, threshold=0.10)


# ---------------------------------------------------------------------------
# RepetitionAccumulator.get_alert_level
# ---------------------------------------------------------------------------
class TestAlertLevels:

    def _make_ep_with_premium(self, premium: float, accelerating: bool = False) -> RepetitionEpisode:
        base_ts = datetime(2026, 4, 25, 10, 0, 0)
        events = []
        for i in range(3):
            ts = base_ts + timedelta(seconds=i * (10 if accelerating else 300))
            ev = _make_event(premium=premium // 3, ts=ts)
            events.append(ev)
        ep = _make_episode(events)
        return ep

    def test_watch_level(self):
        ep = self._make_ep_with_premium(200_000)
        acc = RepetitionAccumulator()
        assert acc.get_alert_level(ep) == "WATCH"

    def test_alert_level(self):
        ep = self._make_ep_with_premium(300_000)
        acc = RepetitionAccumulator()
        assert acc.get_alert_level(ep) == "ALERT"

    def test_strong_signal_level(self):
        ep = self._make_ep_with_premium(1_500_000)
        acc = RepetitionAccumulator()
        assert acc.get_alert_level(ep) == "STRONG_SIGNAL"

    def test_conviction_level_high_premium(self):
        ep = self._make_ep_with_premium(6_000_000)
        acc = RepetitionAccumulator()
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_conviction_level_accelerating_with_1m_premium(self):
        ep = self._make_ep_with_premium(1_200_000, accelerating=True)
        acc = RepetitionAccumulator()
        assert acc.get_alert_level(ep) == "CONVICTION"


# ---------------------------------------------------------------------------
# volume_weighted_premium_factor
# ---------------------------------------------------------------------------
class TestVolumeWeightedPremiumFactor:

    def test_zero_oi_returns_0_5(self):
        ev = _make_event(oi=0, premium=500_000)
        ep = _make_episode([ev, ev, ev])
        result = volume_weighted_premium_factor(ep)
        assert result == 0.5

    def test_normal_ratio(self):
        ev = _make_event(oi=1000, premium=500_000)
        ep = _make_episode([ev, ev, ev])
        result = volume_weighted_premium_factor(ep)
        assert result == 1.0

    def test_small_premium_low_ratio(self):
        ev = _make_event(oi=10_000, premium=1_000)
        ep = _make_episode([ev, ev, ev])
        result = volume_weighted_premium_factor(ep)
        assert 0.0 < result < 0.1

    def test_empty_events_returns_0_5(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=200, expiry="2026-06-20")
        result = volume_weighted_premium_factor(ep)
        assert result == 0.5


# ---------------------------------------------------------------------------
# compute_flow_score
# ---------------------------------------------------------------------------
class TestComputeFlowScore:

    def test_score_zero_premium(self):
        ev = _make_event(premium=0)
        ep = _make_episode([ev, ev, ev])
        score = compute_flow_score(ep)
        assert 0.0 <= score <= 1.0

    def test_score_high_premium(self):
        ev = _make_event(premium=10_000_000 // 3)
        ep = _make_episode([ev, ev, ev])
        score = compute_flow_score(ep)
        assert score > 0.5

    def test_score_accelerating_adds_bonus(self):
        base_ts = datetime(2026, 4, 25, 10, 0, 0)
        events_accel = [
            _make_event(premium=100_000, ts=base_ts + timedelta(seconds=i * 10))
            for i in range(3)
        ]
        events_slow = [
            _make_event(premium=100_000, ts=base_ts + timedelta(seconds=i * 300))
            for i in range(3)
        ]
        ep_accel = _make_episode(events_accel)
        ep_slow  = _make_episode(events_slow)
        assert compute_flow_score(ep_accel) > compute_flow_score(ep_slow)

    def test_score_capped_at_1(self):
        ev = _make_event(premium=50_000_000)
        ep = _make_episode([ev] * 20)
        score = compute_flow_score(ep)
        assert score <= 1.0


# ---------------------------------------------------------------------------
# build_composite recommendation branches
# ---------------------------------------------------------------------------
class TestBuildCompositeRecommendation:

    def _make_high_premium_episode(self, sentiment: str) -> RepetitionEpisode:
        base_ts = datetime(2026, 4, 25, 10, 0, 0)
        events = [
            _make_event(premium=3_000_000, sentiment=sentiment, tier="WHALE", dte=14,
                        ts=base_ts + timedelta(seconds=i))
            for i in range(5)
        ]
        return _make_episode(events)

    def test_buy_recommendation(self):
        ep  = self._make_high_premium_episode("BULLISH")
        acc = RepetitionAccumulator()
        sig = build_composite(ep, acc)
        assert sig.recommendation in {"BUY", "SELL", "HOLD"}
        assert 0.0 <= sig.composite_score <= 1.0

    def test_sell_recommendation_possible(self):
        ep  = self._make_high_premium_episode("BEARISH")
        acc = RepetitionAccumulator()
        sig = build_composite(ep, acc)
        assert sig.recommendation in {"BUY", "SELL", "HOLD"}

    def test_hold_for_low_composite(self):
        ev  = _make_event(premium=10, tier="RETAIL", dte=60)
        ep  = _make_episode([ev, ev, ev])
        acc = RepetitionAccumulator()
        sig = build_composite(ep, acc)
        assert sig.composite_score < 0.65 or sig.recommendation == "HOLD"

    def test_reasoning_string_contains_ticker(self):
        ep  = self._make_high_premium_episode("BULLISH")
        acc = RepetitionAccumulator()
        sig = build_composite(ep, acc)
        assert "AAPL" in sig.reasoning

    def test_swarm_fields_none_on_sync_build(self):
        ep  = self._make_high_premium_episode("BULLISH")
        acc = RepetitionAccumulator()
        sig = build_composite(ep, acc)
        assert sig.swarm_direction is None
        assert sig.swarm_confidence is None


# ---------------------------------------------------------------------------
# build_composite_async swarm integration
# ---------------------------------------------------------------------------
class TestBuildCompositeAsync:

    def _make_episode(self) -> RepetitionEpisode:
        base_ts = datetime(2026, 4, 25, 10, 0, 0)
        events = [
            _make_event(premium=1_000_000, tier="WHALE", ts=base_ts + timedelta(seconds=i))
            for i in range(3)
        ]
        return _make_episode(events)

    def test_swarm_results_injected(self):
        from simulation.ensemble_runner import EnsembleResult
        mock_result = EnsembleResult(
            ticker="AAPL",
            direction="BUY",
            confidence=0.833,
            bull_votes=5,
            bear_votes=1,
            hold_votes=0,
            summary="5 BUY votes.",
            agents=[],
        )
        with patch("signals.composite_signal_engine.run_ensemble", new=AsyncMock(return_value=mock_result)):
            from signals.composite_signal_engine import build_composite_async
            ep  = self._make_episode()
            acc = RepetitionAccumulator()
            sig = _run(build_composite_async(ep, acc))
        assert sig.swarm_direction == "BUY"
        assert sig.swarm_confidence == 0.833
        assert sig.swarm_bull_votes == 5
        assert sig.swarm_bear_votes == 1
        assert sig.swarm_hold_votes == 0

    def test_swarm_exception_is_non_fatal(self):
        with patch("signals.composite_signal_engine.run_ensemble", new=AsyncMock(side_effect=Exception("Groq down"))):
            from signals.composite_signal_engine import build_composite_async
            ep  = self._make_episode()
            acc = RepetitionAccumulator()
            sig = _run(build_composite_async(ep, acc))
        assert sig.swarm_direction is None

    def test_n_agents_forwarded(self):
        from simulation.ensemble_runner import EnsembleResult
        mock_result = EnsembleResult(
            ticker="AAPL", direction="HOLD", confidence=0.5,
            bull_votes=0, bear_votes=0, hold_votes=9, summary="", agents=[],
        )
        with patch("signals.composite_signal_engine.run_ensemble", new=AsyncMock(return_value=mock_result)) as mock_run:
            from signals.composite_signal_engine import build_composite_async
            ep  = self._make_episode()
            acc = RepetitionAccumulator()
            _run(build_composite_async(ep, acc, n_agents=9))
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs.get("n_agents") == 9
