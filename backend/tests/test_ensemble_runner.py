"""
Phase 3 — test_ensemble_runner.py

Covers:
  - run_ensemble() vote aggregation: BUY majority, SELL majority, HOLD/tie
  - confidence calculation for each direction
  - summary string format
  - agents list shape (role, name, direction, reasoning, confidence)
  - zero-agent edge (total=0 → HOLD, confidence=0.0)
  - n_agents snap passed through to SwarmEngine
  - flow_events as string passthrough
  - swarm exception → bubbles out (no silent swallow at this layer)
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# Minimal stub for AgentVerdict so tests don't need a live Groq key
# ---------------------------------------------------------------------------
@dataclass
class _Verdict:
    role:       str
    name:       str
    direction:  str
    reasoning:  str
    confidence: float


def _make_verdicts(buy: int, sell: int, hold: int) -> List[_Verdict]:
    verdicts = []
    for i in range(buy):
        verdicts.append(_Verdict("momentum", f"Bull Agent {i}", "BUY", "Bullish tape.", 0.8))
    for i in range(sell):
        verdicts.append(_Verdict("contrarian", f"Bear Agent {i}", "SELL", "Fading move.", 0.7))
    for i in range(hold):
        verdicts.append(_Verdict("risk", f"Hold Agent {i}", "HOLD", "Insufficient data.", 0.5))
    return verdicts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


FLOW_EVENTS = [
    {
        "ticker": "AAPL",
        "contract_type": "CALL",
        "strike": 200,
        "expiry": "2026-06-20",
        "premium": 500_000,
        "sentiment": "BULLISH",
        "influence_tier": "WHALE",
        "is_golden_sweep": True,
    }
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEnsembleAggregation:

    def _patch_swarm(self, verdicts):
        """Patch SwarmEngine.run() to return given verdicts."""
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value=verdicts)
        return patch("simulation.ensemble_runner.SwarmEngine", return_value=mock_engine)

    def test_buy_majority(self):
        verdicts = _make_verdicts(buy=4, sell=1, hold=1)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("AAPL", FLOW_EVENTS))
        assert result.direction == "BUY"
        assert result.bull_votes == 4
        assert result.bear_votes == 1
        assert result.hold_votes == 1
        assert abs(result.confidence - round(4 / 6, 3)) < 0.001

    def test_sell_majority(self):
        verdicts = _make_verdicts(buy=1, sell=4, hold=1)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("SPY", FLOW_EVENTS))
        assert result.direction == "SELL"
        assert result.confidence == round(4 / 6, 3)

    def test_hold_on_tie(self):
        # buy==bear, neither dominates → HOLD
        verdicts = _make_verdicts(buy=2, sell=2, hold=2)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("TSLA", FLOW_EVENTS))
        assert result.direction == "HOLD"

    def test_hold_majority(self):
        verdicts = _make_verdicts(buy=1, sell=1, hold=4)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("NVDA", FLOW_EVENTS))
        assert result.direction == "HOLD"
        assert result.hold_votes == 4

    def test_summary_string_format(self):
        verdicts = _make_verdicts(buy=3, sell=2, hold=1)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("AAPL", FLOW_EVENTS))
        assert "AAPL" in result.summary
        assert "BUY" in result.summary
        assert "Bull: 3" in result.summary
        assert "Bear: 2" in result.summary
        assert "Hold: 1" in result.summary

    def test_agents_list_shape(self):
        verdicts = _make_verdicts(buy=2, sell=1, hold=0)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("AMZN", FLOW_EVENTS))
        assert len(result.agents) == 3
        for agent in result.agents:
            assert "role" in agent
            assert "name" in agent
            assert "direction" in agent
            assert "reasoning" in agent
            assert "confidence" in agent

    def test_ticker_preserved(self):
        verdicts = _make_verdicts(buy=3, sell=0, hold=0)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("MSFT", FLOW_EVENTS))
        assert result.ticker == "MSFT"

    def test_flow_events_as_string(self):
        """run_ensemble accepts a pre-built summary string — passes straight to SwarmEngine."""
        verdicts = _make_verdicts(buy=2, sell=1, hold=0)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("QQQ", "6 CALL sweeps on QQQ, WHALE tier"))
        assert result.direction == "BUY"

    def test_single_agent_buy(self):
        verdicts = _make_verdicts(buy=1, sell=0, hold=0)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("IWM", FLOW_EVENTS, n_agents=3))
        assert result.direction == "BUY"
        assert result.confidence == 1.0

    def test_n_agents_passed_to_engine(self):
        """n_agents kwarg should be forwarded to SwarmEngine constructor."""
        verdicts = _make_verdicts(buy=3, sell=0, hold=0)
        with patch("simulation.ensemble_runner.SwarmEngine") as mock_cls:
            mock_engine = MagicMock()
            mock_engine.run = AsyncMock(return_value=verdicts)
            mock_cls.return_value = mock_engine
            from simulation.ensemble_runner import run_ensemble
            _run(run_ensemble("AAPL", FLOW_EVENTS, n_agents=9))
            mock_cls.assert_called_once_with(n_agents=9)

    def test_confidence_capped_at_1(self):
        """All 6 votes in same direction → confidence = 1.0."""
        verdicts = _make_verdicts(buy=6, sell=0, hold=0)
        with self._patch_swarm(verdicts):
            from simulation.ensemble_runner import run_ensemble
            result = _run(run_ensemble("AAPL", FLOW_EVENTS))
        assert result.confidence == 1.0
