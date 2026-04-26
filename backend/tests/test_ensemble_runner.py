"""
Regression tests for simulation/ensemble_runner.py

Strategy:
  - SwarmEngine.run() is patched to return controlled AgentVerdict lists,
    so no LLM calls are made.
  - Tests focus on vote counting, direction resolution, confidence calculation,
    summary string content, and EnsembleResult field mapping.

Covers:
  EnsembleResult dataclass:
  - All fields present (ticker, direction, confidence, bull_votes, bear_votes,
    hold_votes, summary, agents)
  - agents defaults to empty list

  Vote aggregation:
  - BUY majority -> direction=BUY, confidence=bull/total
  - SELL majority -> direction=SELL, confidence=bear/total
  - HOLD majority -> direction=HOLD, confidence=hold/total
  - Tie (bull==bear, no majority) -> direction=HOLD
  - Zero verdicts -> no ZeroDivisionError (total clamped to 1)

  Summary string:
  - Contains ticker
  - Contains agent count
  - Contains bull/bear/hold counts
  - Contains final direction
  - Contains confidence percentage

  agents list:
  - Each agent dict has role, name, direction, reasoning, confidence
  - Correct number of agent dicts in result

  Wiring:
  - SwarmEngine constructed with correct n_agents
  - engine.run() called with correct ticker and flow_events
  - flow_events list forwarded unchanged
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from dataclasses import fields

from simulation.ensemble_runner import run_ensemble, EnsembleResult
from simulation.swarm_engine import AgentVerdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verdict(direction: str, role: str = "momentum", name: str = "Agent",
             reasoning: str = "test", confidence: float = 0.8) -> AgentVerdict:
    return AgentVerdict(
        role=role, name=name, direction=direction,
        reasoning=reasoning, confidence=confidence,
    )


def _patch_swarm(verdicts: list):
    """Patch SwarmEngine so run() returns the given verdicts list."""
    mock_engine = MagicMock()
    mock_engine.run = AsyncMock(return_value=verdicts)
    return patch(
        "simulation.ensemble_runner.SwarmEngine",
        return_value=mock_engine,
    ), mock_engine


# ---------------------------------------------------------------------------
# EnsembleResult dataclass
# ---------------------------------------------------------------------------

def test_ensemble_result_has_all_fields():
    field_names = {f.name for f in fields(EnsembleResult)}
    for name in ("ticker", "direction", "confidence",
                 "bull_votes", "bear_votes", "hold_votes", "summary", "agents"):
        assert name in field_names


def test_ensemble_result_agents_default_empty():
    r = EnsembleResult(
        ticker="X", direction="HOLD", confidence=0.5,
        bull_votes=0, bear_votes=0, hold_votes=1,
        summary="test",
    )
    assert r.agents == []


# ---------------------------------------------------------------------------
# Vote aggregation — direction + confidence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buy_majority_sets_direction_buy():
    verdicts = [_verdict("BUY")] * 4 + [_verdict("SELL")] + [_verdict("HOLD")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("AAPL", [], n_agents=6)
    assert result.direction == "BUY"


@pytest.mark.asyncio
async def test_buy_majority_confidence_correct():
    verdicts = [_verdict("BUY")] * 4 + [_verdict("SELL")] * 2
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("AAPL", [], n_agents=6)
    assert result.confidence == round(4 / 6, 3)


@pytest.mark.asyncio
async def test_sell_majority_sets_direction_sell():
    verdicts = [_verdict("SELL")] * 4 + [_verdict("BUY")] + [_verdict("HOLD")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("TSLA", [], n_agents=6)
    assert result.direction == "SELL"


@pytest.mark.asyncio
async def test_sell_majority_confidence_correct():
    verdicts = [_verdict("SELL")] * 5 + [_verdict("HOLD")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("TSLA", [], n_agents=6)
    assert result.confidence == round(5 / 6, 3)


@pytest.mark.asyncio
async def test_hold_majority_sets_direction_hold():
    verdicts = [_verdict("HOLD")] * 4 + [_verdict("BUY")] + [_verdict("SELL")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("NVDA", [], n_agents=6)
    assert result.direction == "HOLD"


@pytest.mark.asyncio
async def test_bull_bear_tie_resolves_to_hold():
    """When bull == bear and neither dominates hold, result is HOLD."""
    verdicts = [_verdict("BUY")] * 3 + [_verdict("SELL")] * 3
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("SPY", [], n_agents=6)
    assert result.direction == "HOLD"


@pytest.mark.asyncio
async def test_zero_verdicts_no_division_error():
    """Empty verdict list -> total clamped to 1, no ZeroDivisionError."""
    patcher, _ = _patch_swarm([])
    with patcher:
        result = await run_ensemble("QQQ", [], n_agents=3)
    assert result.direction == "HOLD"
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Vote counts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bull_votes_counted_correctly():
    verdicts = [_verdict("BUY")] * 3 + [_verdict("SELL")] * 2 + [_verdict("HOLD")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("AAPL", [], n_agents=6)
    assert result.bull_votes == 3


@pytest.mark.asyncio
async def test_bear_votes_counted_correctly():
    verdicts = [_verdict("BUY")] * 2 + [_verdict("SELL")] * 3 + [_verdict("HOLD")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("AAPL", [], n_agents=6)
    assert result.bear_votes == 3


@pytest.mark.asyncio
async def test_hold_votes_counted_correctly():
    verdicts = [_verdict("BUY")] + [_verdict("SELL")] + [_verdict("HOLD")] * 4
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("MSFT", [], n_agents=6)
    assert result.hold_votes == 4


# ---------------------------------------------------------------------------
# Summary string content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_contains_ticker():
    patcher, _ = _patch_swarm([_verdict("BUY")] * 6)
    with patcher:
        result = await run_ensemble("NVDA", [], n_agents=6)
    assert "NVDA" in result.summary


@pytest.mark.asyncio
async def test_summary_contains_direction():
    patcher, _ = _patch_swarm([_verdict("SELL")] * 6)
    with patcher:
        result = await run_ensemble("AMD", [], n_agents=6)
    assert "SELL" in result.summary


@pytest.mark.asyncio
async def test_summary_contains_vote_counts():
    verdicts = [_verdict("BUY")] * 3 + [_verdict("SELL")] * 2 + [_verdict("HOLD")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("META", [], n_agents=6)
    assert "3" in result.summary  # bull count
    assert "2" in result.summary  # bear count


# ---------------------------------------------------------------------------
# agents list mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agents_list_length_matches_verdicts():
    verdicts = [_verdict("BUY", role=f"role_{i}", name=f"Agent-{i}") for i in range(6)]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("AAPL", [], n_agents=6)
    assert len(result.agents) == 6


@pytest.mark.asyncio
async def test_agents_dicts_have_required_keys():
    verdicts = [_verdict("HOLD", role="risk", name="Risk Manager")]
    patcher, _ = _patch_swarm(verdicts)
    with patcher:
        result = await run_ensemble("AAPL", [], n_agents=3)
    agent = result.agents[0]
    for key in ("role", "name", "direction", "reasoning", "confidence"):
        assert key in agent


@pytest.mark.asyncio
async def test_agents_fields_mapped_correctly():
    v = _verdict("BUY", role="momentum", name="Momentum Trader",
                 reasoning="Strong sweep", confidence=0.9)
    patcher, _ = _patch_swarm([v])
    with patcher:
        result = await run_ensemble("AAPL", [], n_agents=3)
    a = result.agents[0]
    assert a["role"]       == "momentum"
    assert a["name"]       == "Momentum Trader"
    assert a["direction"]  == "BUY"
    assert a["reasoning"]  == "Strong sweep"
    assert a["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Wiring: n_agents and flow_events forwarded correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_swarm_engine_constructed_with_n_agents():
    with patch("simulation.ensemble_runner.SwarmEngine") as MockEngine:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=[])
        MockEngine.return_value = mock_instance
        await run_ensemble("AAPL", [], n_agents=9)
    MockEngine.assert_called_once_with(n_agents=9)


@pytest.mark.asyncio
async def test_engine_run_called_with_ticker_and_flow_events():
    flow = [{"ticker": "AAPL", "contract_type": "CALL"}]
    with patch("simulation.ensemble_runner.SwarmEngine") as MockEngine:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=[])
        MockEngine.return_value = mock_instance
        await run_ensemble("AAPL", flow, n_agents=6)
    mock_instance.run.assert_called_once_with("AAPL", flow)
