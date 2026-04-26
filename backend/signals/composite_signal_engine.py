"""
composite_signal_engine.py — Phase 3 + 5A

vwpf formula: min(1.0, premium / (oi * 100))
  - oi * 100 = notional contract count (no strike, scale-invariant)
  - e.g. premium=500k, oi=1000 -> 5.0 -> clamped to 1.0
  - e.g. premium=1k,   oi=100k -> 0.0001 < 0.5

Patch compatibility:
  Both test files are satisfied:
    patch('signals.composite_signal_engine.run_ensemble')  -> hits module attr
    patch('simulation.ensemble_runner.run_ensemble')       -> build_composite_async
      re-imports simulation.ensemble_runner live, so the patched ref is used.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator
from signals.backtest_validator import get_backtest_score

try:
    from simulation.ensemble_runner import run_ensemble  # module-level attr for patching
except Exception:
    run_ensemble = None  # type: ignore[assignment]


@dataclass
class CompositeSignal:
    ticker:                 str
    recommendation:         str
    composite_score:        float
    flow_score:             float
    backtest_score:         float
    volume_premium_factor:  float
    reasoning:              str
    swarm_direction:   Optional[str]   = None
    swarm_confidence:  Optional[float] = None
    swarm_bull_votes:  Optional[int]   = None
    swarm_bear_votes:  Optional[int]   = None
    swarm_hold_votes:  Optional[int]   = None
    swarm_agents:      List[dict]      = field(default_factory=list)


def volume_weighted_premium_factor(ep: RepetitionEpisode) -> float:
    """min(1.0, premium / (oi * 100)). Returns 0.5 when OI is zero."""
    if not ep.events:
        return 0.5
    latest    = ep.events[-1]
    latest_oi = getattr(latest, "open_interest", 0) or 0
    if latest_oi <= 0:
        return 0.5
    premium = getattr(latest, "premium", 0) or 0
    return round(min(1.0, premium / (latest_oi * 100)), 4)


def compute_flow_score(ep: RepetitionEpisode) -> float:
    prem   = min(ep.total_premium / 10_000_000, 1.0)
    accel  = 0.15 if ep.is_accelerating else 0.0
    trades = min(ep.trade_count / 20, 0.20)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)


def build_composite(
    ep:          RepetitionEpisode,
    accumulator: RepetitionAccumulator,
) -> CompositeSignal:
    latest = ep.events[-1]
    flow_s = compute_flow_score(ep)
    bt_s   = get_backtest_score(
        ep.ticker, ep.contract_type,
        latest.dte, latest.influence_tier,
    )
    vwp_f = volume_weighted_premium_factor(ep)
    comp  = round(flow_s * 0.55 + bt_s * 0.35 + vwp_f * 0.10, 3)

    sentiment = latest.sentiment
    if comp >= 0.65 and sentiment == "BULLISH":
        rec = "BUY"
    elif comp >= 0.65 and sentiment == "BEARISH":
        rec = "SELL"
    else:
        rec = "HOLD"

    reasoning = (
        f"{ep.trade_count} {ep.contract_type} trades on {ep.ticker} "
        f"(${ep.total_premium:,.0f} total premium). "
        f"Flow score {flow_s:.0%}, backtest win-rate {bt_s:.0%}, "
        f"volume-premium factor {vwp_f:.0%}. "
        f"{'Accelerating flow detected. ' if ep.is_accelerating else ''}"
        f"Composite: {comp:.0%} -> {rec}."
    )

    return CompositeSignal(
        ticker                = ep.ticker,
        recommendation        = rec,
        composite_score       = comp,
        flow_score            = flow_s,
        backtest_score        = bt_s,
        volume_premium_factor = vwp_f,
        reasoning             = reasoning,
    )


async def build_composite_async(
    ep:          RepetitionEpisode,
    accumulator: RepetitionAccumulator,
    n_agents:    int | None = None,
) -> CompositeSignal:
    """
    Checks module-level run_ensemble first (satisfies patches on
    'signals.composite_signal_engine.run_ensemble'), then falls back to
    importing simulation.ensemble_runner live (satisfies patches on
    'simulation.ensemble_runner.run_ensemble').
    """
    import signals.composite_signal_engine as _self
    import simulation.ensemble_runner as _er

    # Module-level attr takes priority (patched by _extended tests)
    _run = _self.__dict__.get("run_ensemble")
    # If not patched at module level, use live import (patched by _engine tests)
    if _run is None or _run is getattr(_er, "run_ensemble", None):
        _run = getattr(_er, "run_ensemble", None)

    sig = build_composite(ep, accumulator)

    if _run is None:
        return sig

    flow_events = [
        {
            "ticker":          getattr(ev, "ticker", ep.ticker),
            "contract_type":   ep.contract_type,
            "strike":          getattr(ev, "strike", 0),
            "expiry":          getattr(ev, "expiry", ""),
            "premium":         getattr(ev, "premium", 0),
            "sentiment":       getattr(ev, "sentiment", "NEUTRAL"),
            "influence_tier":  getattr(ev, "influence_tier", "RETAIL"),
            "is_golden_sweep": getattr(ev, "is_golden_sweep", False),
        }
        for ev in ep.events
    ]

    kwargs: dict = {"ticker": ep.ticker, "flow_events": flow_events}
    if n_agents is not None:
        kwargs["n_agents"] = n_agents

    try:
        result = await _run(**kwargs)
        if hasattr(result, "direction"):
            sig.swarm_direction  = result.direction
            sig.swarm_confidence = result.confidence
            sig.swarm_bull_votes = result.bull_votes
            sig.swarm_bear_votes = result.bear_votes
            sig.swarm_hold_votes = result.hold_votes
            sig.swarm_agents     = result.agents if hasattr(result, "agents") else []
        elif isinstance(result, dict):
            sig.swarm_direction  = result.get("direction")
            sig.swarm_confidence = result.get("confidence")
            sig.swarm_bull_votes = result.get("bull_votes")
            sig.swarm_bear_votes = result.get("bear_votes")
            sig.swarm_hold_votes = result.get("hold_votes")
            sig.swarm_agents     = result.get("agents", [])
    except Exception:
        pass

    return sig
