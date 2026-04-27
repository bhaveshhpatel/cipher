"""
P3 coverage tests for signals/composite_signal_engine.py.

Targets uncovered lines:
  - Lines 22-23: _original_run_ensemble = None when ensemble import fails
  - Lines 159-165: build_composite_async with _run=None returns base sig, swarm fields all None
  - Swarm result as object with .direction attr → swarm fields populated
  - Swarm result as dict → swarm fields populated
  - Swarm _run raises exception → exception swallowed, base sig returned intact
"""
from unittest.mock import MagicMock, AsyncMock, patch
from signals.composite_signal_engine import (
    build_composite_async,
    CompositeSignal,
)
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator


def _make_ep(ticker="AAPL"):
    ev = MagicMock()
    ev.ticker          = ticker
    ev.dte             = 7
    ev.influence_tier  = "WHALE"
    ev.sentiment       = "BULLISH"
    ev.premium         = 600_000
    ev.open_interest   = 5000
    ev.strike          = 150.0
    ev.expiry          = "2026-05-01"
    ev.is_golden_sweep = True
    ep = MagicMock(spec=RepetitionEpisode)
    ep.ticker          = ticker
    ep.contract_type   = "CALL"
    ep.total_premium   = 600_000
    ep.trade_count     = 5
    ep.is_accelerating = True
    ep.events          = [ev]
    return ep


def _acc():
    return MagicMock(spec=RepetitionAccumulator)


# ---------------------------------------------------------------------------
# _run is None — import failed, return base signal
# ---------------------------------------------------------------------------

async def test_run_none_returns_base_signal():
    ep = _make_ep()
    with patch("signals.composite_signal_engine._original_run_ensemble", None), \
         patch("signals.composite_signal_engine.run_ensemble", None), \
         patch("simulation.ensemble_runner.run_ensemble", None):
        sig = await build_composite_async(ep, _acc())
    assert isinstance(sig, CompositeSignal)
    assert sig.swarm_direction is None


# ---------------------------------------------------------------------------
# Swarm result as object with .direction
# ---------------------------------------------------------------------------

async def test_swarm_object_result_populates_fields():
    ep      = _make_ep()
    swarm   = MagicMock()
    swarm.direction  = "BULLISH"
    swarm.confidence = 0.8
    swarm.bull_votes = 7
    swarm.bear_votes = 2
    swarm.hold_votes = 1
    swarm.agents     = [{"id": 1}]
    sentinel = object()
    mock_run = AsyncMock(return_value=swarm)
    with patch("signals.composite_signal_engine.run_ensemble", mock_run), \
         patch("signals.composite_signal_engine._original_run_ensemble", sentinel):
        sig = await build_composite_async(ep, _acc())
    assert sig.swarm_direction  == "BULLISH"
    assert sig.swarm_confidence == 0.8
    assert sig.swarm_bull_votes == 7


# ---------------------------------------------------------------------------
# Swarm result as dict
# ---------------------------------------------------------------------------

async def test_swarm_dict_result_populates_fields():
    ep       = _make_ep()
    sentinel = object()
    mock_run = AsyncMock(return_value={
        "direction": "BEARISH", "confidence": 0.6,
        "bull_votes": 1, "bear_votes": 8, "hold_votes": 1, "agents": [],
    })
    with patch("signals.composite_signal_engine.run_ensemble", mock_run), \
         patch("signals.composite_signal_engine._original_run_ensemble", sentinel):
        sig = await build_composite_async(ep, _acc())
    assert sig.swarm_direction  == "BEARISH"
    assert sig.swarm_bear_votes == 8


# ---------------------------------------------------------------------------
# Swarm raises → exception swallowed, base signal returned
# ---------------------------------------------------------------------------

async def test_swarm_exception_swallowed():
    ep       = _make_ep()
    sentinel = object()
    mock_run = AsyncMock(side_effect=RuntimeError("swarm exploded"))
    with patch("signals.composite_signal_engine.run_ensemble", mock_run), \
         patch("signals.composite_signal_engine._original_run_ensemble", sentinel):
        sig = await build_composite_async(ep, _acc())
    assert isinstance(sig, CompositeSignal)
    assert sig.swarm_direction is None
