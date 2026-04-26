"""
Regression tests for simulation/ensemble_runner.py

Covers (matched to actual source):
  - run_ensemble() returns EnsembleResult dataclass
  - EnsembleResult has all required fields: ticker/direction/confidence/bull_votes/
    bear_votes/hold_votes/summary/agents
  - Bull majority → direction='BUY', confidence = bull/total
  - Bear majority → direction='SELL', confidence = bear/total
  - Tie (no clear majority) → direction='HOLD'
  - All votes equal → direction='HOLD' (hold >= bull and hold >= bear)
  - confidence is rounded to 3 decimal places
  - summary string contains ticker, direction, and 'confidence'
  - summary string contains individual vote counts
  - agents list has correct length matching n_agents
  - each agent dict has keys: role, name, direction, reasoning, confidence
  - n_agents=3 → 3 agents, n_agents=9 → 9 agents (snapping tested in swarm tests)
  - no-API-key path → all agents return HOLD → direction='HOLD'
  - flow_events as string (pre-built summary) is accepted
  - flow_events as list of dicts is accepted
  - empty flow_events list is accepted (graceful)
"""
import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from dataclasses import asdict

from simulation.ensemble_runner import run_ensemble, EnsembleResult
from simulation.swarm_engine import AgentVerdict


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_verdict(direction: str, role: str = "momentum", name: str = "Momentum Trader") -> AgentVerdict:
    return AgentVerdict(
        role=role,
        name=name,
        direction=direction,
        reasoning="Test reasoning.",
        confidence=0.8,
    )


def _patch_swarm(verdicts: list):
    """Patch SwarmEngine.run() to return the given verdicts synchronously."""
    async def _fake_run(ticker, flow_events):
        return verdicts
    return patch("simulation.ensemble_runner.SwarmEngine.run", new=_fake_run)


# ── EnsembleResult shape ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_ensemble_returns_ensemble_result():
    verdicts = [_make_verdict("BUY")] * 4 + [_make_verdict("SELL")] * 2
    with _patch_swarm(verdicts):
        result = await run_ensemble("AAPL", [])
    assert isinstance(result, EnsembleResult)


@pytest.mark.asyncio
async def test_ensemble_result_has_all_fields():
    verdicts = [_make_verdict("BUY")] * 4 + [_make_verdict("SELL")] * 2
    with _patch_swarm(verdicts):
        result = await run_ensemble("AAPL", [])
    d = asdict(result)
    for key in ("ticker", "direction", "confidence", "bull_votes",
                "bear_votes", "hold_votes", "summary", "agents"):
        assert key in d, f"EnsembleResult missing field: '{key}'"


@pytest.mark.asyncio
async def test_ensemble_ticker_is_preserved():
    with _patch_swarm([_make_verdict("BUY")] * 3):
        result = await run_ensemble("TSLA", [])
    assert result.ticker == "TSLA"


# ── vote aggregation: BUY wins ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bull_majority_direction_is_buy():
    verdicts = [_make_verdict("BUY")] * 4 + [_make_verdict("SELL")] * 1 + [_make_verdict("HOLD")] * 1
    with _patch_swarm(verdicts):
        result = await run_ensemble("AAPL", [])
    assert result.direction == "BUY"
    assert result.bull_votes == 4


@pytest.mark.asyncio
async def test_bull_majority_confidence_is_bull_share():
    verdicts = [_make_verdict("BUY")] * 4 + [_make_verdict("SELL")] * 2
    with _patch_swarm(verdicts):
        result = await run_ensemble("AAPL", [])
    expected = round(4 / 6, 3)
    assert result.confidence == expected


# ── vote aggregation: SELL wins ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bear_majority_direction_is_sell():
    verdicts = [_make_verdict("SELL")] * 5 + [_make_verdict("BUY")] * 1
    with _patch_swarm(verdicts):
        result = await run_ensemble("SPY", [])
    assert result.direction == "SELL"
    assert result.bear_votes == 5


@pytest.mark.asyncio
async def test_bear_majority_confidence_is_bear_share():
    verdicts = [_make_verdict("SELL")] * 3 + [_make_verdict("BUY")] * 1 + [_make_verdict("HOLD")] * 2
    with _patch_swarm(verdicts):
        result = await run_ensemble("QQQ", [])
    expected = round(3 / 6, 3)
    assert result.confidence == expected


# ── vote aggregation: HOLD wins (tie / no clear majority) ────────────────────

@pytest.mark.asyncio
async def test_tie_vote_direction_is_hold():
    """Equal BUY and SELL with no HOLD → falls through to HOLD branch."""
    verdicts = [_make_verdict("BUY")] * 3 + [_make_verdict("SELL")] * 3
    with _patch_swarm(verdicts):
        result = await run_ensemble("NVDA", [])
    assert result.direction == "HOLD"


@pytest.mark.asyncio
async def test_all_hold_votes_direction_is_hold():
    verdicts = [_make_verdict("HOLD")] * 6
    with _patch_swarm(verdicts):
        result = await run_ensemble("META", [])
    assert result.direction == "HOLD"
    assert result.hold_votes == 6


# ── confidence precision ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confidence_rounded_to_3_decimal_places():
    # 2/6 = 0.333...
    verdicts = [_make_verdict("BUY")] * 2 + [_make_verdict("SELL")] * 4
    with _patch_swarm(verdicts):
        result = await run_ensemble("AAPL", [])
    # confidence should be a float with at most 3 decimal digits
    assert result.confidence == round(result.confidence, 3)


# ── summary string ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_contains_ticker():
    with _patch_swarm([_make_verdict("BUY")] * 3):
        result = await run_ensemble("AAPL", [])
    assert "AAPL" in result.summary


@pytest.mark.asyncio
async def test_summary_contains_direction():
    with _patch_swarm([_make_verdict("BUY")] * 6):
        result = await run_ensemble("SPY", [])
    assert "BUY" in result.summary


@pytest.mark.asyncio
async def test_summary_contains_vote_counts():
    verdicts = [_make_verdict("BUY")] * 4 + [_make_verdict("SELL")] * 2
    with _patch_swarm(verdicts):
        result = await run_ensemble("TSLA", [])
    assert "4" in result.summary   # bull count
    assert "2" in result.summary   # bear count


# ── agents list ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agents_list_length_matches_verdicts():
    verdicts = [
        AgentVerdict(role=f"role_{i}", name=f"Agent {i}", direction="BUY",
                     reasoning="r", confidence=0.7)
        for i in range(6)
    ]
    with _patch_swarm(verdicts):
        result = await run_ensemble("AAPL", [])
    assert len(result.agents) == 6


@pytest.mark.asyncio
async def test_agents_list_each_dict_has_required_keys():
    verdicts = [AgentVerdict(role="momentum", name="Momentum Trader",
                             direction="BUY", reasoning="r", confidence=0.9)]
    with _patch_swarm(verdicts):
        result = await run_ensemble("SPY", [])
    agent = result.agents[0]
    for key in ("role", "name", "direction", "reasoning", "confidence"):
        assert key in agent, f"Agent dict missing key: '{key}'"


@pytest.mark.asyncio
async def test_agents_list_name_field_is_populated():
    """Phase 5A fix: EnsembleResult agents must include 'name' field."""
    verdicts = [AgentVerdict(role="technical", name="Technical Analyst",
                             direction="SELL", reasoning="bearish setup", confidence=0.75)]
    with _patch_swarm(verdicts):
        result = await run_ensemble("NVDA", [])
    assert result.agents[0]["name"] == "Technical Analyst"


# ── n_agents param ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_n_agents_3_produces_3_agents():
    verdicts = [_make_verdict("BUY")] * 3
    with _patch_swarm(verdicts):
        result = await run_ensemble("AAPL", [], n_agents=3)
    assert len(result.agents) == 3


@pytest.mark.asyncio
async def test_n_agents_9_produces_9_agents():
    verdicts = [_make_verdict("HOLD")] * 9
    with _patch_swarm(verdicts):
        result = await run_ensemble("SPY", [], n_agents=9)
    assert len(result.agents) == 9


# ── no-API-key fallback ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_api_key_all_agents_return_hold():
    """When GROQ_API_KEY is not set, SwarmEngine returns all HOLD verdicts."""
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = None
        ms.SWARM_N_AGENTS = 6
        result = await run_ensemble("AAPL", [])
    assert result.direction == "HOLD"
    assert all(a["direction"] == "HOLD" for a in result.agents)


# ── flow_events input types ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flow_events_as_string_is_accepted():
    with _patch_swarm([_make_verdict("BUY")] * 6):
        result = await run_ensemble("AAPL", "Pre-built summary string.")
    assert result.direction == "BUY"


@pytest.mark.asyncio
async def test_flow_events_as_list_is_accepted():
    events = [{"ticker": "AAPL", "contract_type": "CALL", "premium": 500000,
               "strike": 200, "expiry": "2026-06-20", "sentiment": "BULLISH",
               "influence_tier": "WHALE", "is_golden_sweep": True}]
    with _patch_swarm([_make_verdict("BUY")] * 6):
        result = await run_ensemble("AAPL", events)
    assert result.direction == "BUY"


@pytest.mark.asyncio
async def test_empty_flow_events_does_not_raise():
    with _patch_swarm([_make_verdict("HOLD")] * 6):
        result = await run_ensemble("AAPL", [])
    assert result is not None
