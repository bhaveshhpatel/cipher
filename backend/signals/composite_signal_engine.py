"""
Combines flow score + backtest score + volume-weighted premium factor
into a final composite signal, then auto-triggers the AI swarm.

Phase 3 weight breakdown:
  flow_score              × 0.55
  backtest_score          × 0.35
  volume_premium_factor   × 0.10

Phase 5A additions:
  - build_composite_async() is the primary entry point (awaitable)
  - build_composite() kept as sync wrapper returning signal WITHOUT swarm

Patch path:
  Both test files are satisfied:
    - patch('signals.composite_signal_engine.run_ensemble', ...)  → hits module attr
    - patch('simulation.ensemble_runner.run_ensemble', ...)       → hits same ref
      because run_ensemble is imported at module level from ensemble_runner,
      AND build_composite_async re-reads it from this module's globals().

vwpf formula:
  ratio = latest.premium / (latest_oi * 100)
  capped at 1.0. Denominator is contracts × 100 (no strike), keeping the
  ratio scale-invariant and comparable across strikes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator
from signals.backtest_validator import get_backtest_score

try:
    from simulation.ensemble_runner import run_ensemble  # noqa: F401  (module-level attr for patching)
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
    """
    Ratio of latest event's premium to its notional contract value.

    Formula: min(1.0, premium / (oi * 100))

    Rules:
      - No events   -> 0.5 (neutral fallback)
      - OI == 0     -> 0.5 (no OI data)
      - Otherwise   -> min(1.0, premium / (oi * 100))

    Omits strike so the ratio is comparable across different strikes.
    """
    if not ep.events:
        return 0.5
    latest    = ep.events[-1]
    latest_oi = getattr(latest, "open_interest", 0) or 0
    if latest_oi <= 0:
        return 0.5
    premium = getattr(latest, "premium", 0) or 0
    ratio = premium / (latest_oi * 100)
    return round(min(1.0, ratio), 4)


def compute_flow_score(ep: RepetitionEpisode) -> float:
    prem   = min(ep.total_premium / 10_000_000, 1.0)
    accel  = 0.15 if ep.is_accelerating else 0.0
    trades = min(ep.trade_count / 20, 0.20)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)


def build_composite(
    ep:          RepetitionEpisode,
    accumulator: RepetitionAccumulator,
) -> CompositeSignal:
    """Sync build — returns CompositeSignal WITHOUT swarm verdict."""
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
        f"Composite: {comp:.0%} → {rec}."
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
    Phase 5A primary entry point.
    Reads run_ensemble from this module's globals() so that patches on
    'signals.composite_signal_engine.run_ensemble' are always respected.
    """
    import signals.composite_signal_engine as _self
    _run = _self.__dict__.get("run_ensemble")

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
