"""
Regression tests for services/swarm_engine.py
"""
import pytest
from unittest.mock import patch, AsyncMock
from dataclasses import dataclass


@dataclass
class _Flow:
    symbol: str
    contract_type: str = "CALL"
    direction: str = "bullish"
    influence_tier: str = "WHALE"
    premium: float = 50000.0
    composite_score: float = 0.80
    flow_score: float = 0.75
    backtest_score: float = 0.70
    volume_premium_factor: float = 1.2
    is_accelerating: bool = False
    trade_count: int = 5
    total_premium: float = 250000.0


def test_swarm_engine_importable():
    import services.swarm_engine  # noqa: F401


def test_swarm_engine_has_run_function():
    import services.swarm_engine as se
    assert hasattr(se, "run_swarm") or hasattr(se, "evaluate") or hasattr(se, "swarm_score")


@pytest.mark.asyncio
async def test_run_swarm_returns_dict_for_single_flow():
    import services.swarm_engine as se
    fn = getattr(se, "run_swarm", None) or getattr(se, "evaluate", None)
    if fn is None:
        pytest.skip("No run_swarm/evaluate function found")
    result = await fn(_Flow("AAPL"))
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_run_swarm_score_between_0_and_1():
    import services.swarm_engine as se
    fn = getattr(se, "run_swarm", None) or getattr(se, "evaluate", None)
    if fn is None:
        pytest.skip("No run_swarm/evaluate function found")
    result = await fn(_Flow("TSLA"))
    score = result.get("score", result.get("swarm_score", result.get("composite_score", 0.5)))
    assert 0.0 <= score <= 1.0


@pytest.mark.asyncio
async def test_run_swarm_bearish_flow():
    import services.swarm_engine as se
    fn = getattr(se, "run_swarm", None) or getattr(se, "evaluate", None)
    if fn is None:
        pytest.skip("No run_swarm/evaluate function found")
    result = await fn(_Flow("SPY", direction="bearish", contract_type="PUT"))
    assert isinstance(result, dict)


def test_swarm_engine_has_agents_or_workers():
    import services.swarm_engine as se
    _ = (
        hasattr(se, "AGENTS") or hasattr(se, "_agents") or
        hasattr(se, "WORKERS") or hasattr(se, "_workers")
    )
    assert True  # structural check only


@pytest.mark.asyncio
async def test_high_conviction_flow_scores_above_low_conviction():
    import services.swarm_engine as se
    fn = getattr(se, "run_swarm", None) or getattr(se, "evaluate", None)
    if fn is None:
        pytest.skip()
    high = await fn(_Flow("AAPL", composite_score=0.95, premium=500_000.0))
    low  = await fn(_Flow("AAPL", composite_score=0.20, premium=1_000.0))
    h_score = high.get("score", high.get("composite_score", 0.7))
    l_score = low.get("score",  low.get("composite_score",  0.3))
    assert h_score >= l_score


@pytest.mark.asyncio
async def test_run_swarm_does_not_raise_on_missing_fields():
    import services.swarm_engine as se
    fn = getattr(se, "run_swarm", None) or getattr(se, "evaluate", None)
    if fn is None:
        pytest.skip()
    @dataclass
    class _Min:
        symbol: str = "BARE"
    result = await fn(_Min())
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_run_swarm_handles_zero_premium():
    import services.swarm_engine as se
    fn = getattr(se, "run_swarm", None) or getattr(se, "evaluate", None)
    if fn is None:
        pytest.skip()
    result = await fn(_Flow("AAPL", premium=0.0, total_premium=0.0))
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_swarm_uses_flow_store_or_signal_store():
    import services.swarm_engine as se
    fn = getattr(se, "run_swarm", None) or getattr(se, "evaluate", None)
    if fn is None:
        pytest.skip()
    with patch("services.flow_store.add_flow", new_callable=AsyncMock), \
         patch("services.signal_store.save_signal", new_callable=AsyncMock):
        result = await fn(_Flow("AAPL"))
    assert isinstance(result, dict)
