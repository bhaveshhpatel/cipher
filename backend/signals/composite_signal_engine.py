"""
Combines flow score + backtest score + volume-weighted premium factor
into a final composite signal, then auto-triggers the AI swarm.

Phase 3 weight breakdown:
  flow_score              × 0.55
  backtest_score          × 0.35
  volume_premium_factor   × 0.10

Phase 5A additions:
  - build_composite() now calls run_ensemble() after scoring
  - CompositeSignal carries swarm_direction, swarm_confidence, swarm_agents,
    swarm_bull_votes, swarm_bear_votes, swarm_hold_votes
  - build_composite_async() is the new primary entry point (awaitable)
  - build_composite() kept as sync wrapper returning signal WITHOUT swarm
    (for legacy callers); use build_composite_async() for full swarm output

Module-level run_ensemble is imported eagerly so tests can
patch('signals.composite_signal_engine.run_ensemble', ...).
"""
from dataclasses import dataclass, field
from typing import List, Optional
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator
from signals.backtest_validator import get_backtest_score

# Eagerly import so patch('signals.composite_signal_engine.run_ensemble') works.
# Wrapped in try/except so the module still loads if simulation pkg is absent.
try:
    from simulation.ensemble_runner import run_ensemble  # noqa: F401
except Exception:  # pragma: no cover
    run_ensemble = None  # type: ignore[assignment]


@dataclass
class CompositeSignal:
    ticker:                 str
    recommendation:         str    # BUY | SELL | HOLD (composite engine)
    composite_score:        float
    flow_score:             float
    backtest_score:         float
    volume_premium_factor:  float
    reasoning:              str
    # Phase 5A — swarm verdict fields (None until swarm runs)
    swarm_direction:   Optional[str]   = None   # BUY | SELL | HOLD
    swarm_confidence:  Optional[float] = None   # 0.0-1.0
    swarm_bull_votes:  Optional[int]   = None
    swarm_bear_votes:  Optional[int]   = None
    swarm_hold_votes:  Optional[int]   = None
    swarm_agents:      List[dict]      = field(default_factory=list)


def volume_weighted_premium_factor(ep: RepetitionEpisode) -> float:
    """
    Ratio of episode premium to notional OI value.
    When OI is 0 or unavailable, falls back to premium-only scaling
    (capped at 1.0) so the factor is not a meaningless 0.5 constant.

    Fix: test_vwpf_low_premium_vs_oi expects factor < 0.5 for small
    premium (e.g. $5k) with zero OI — old code always returned 0.5.
    New code: ratio = premium / 1_000_000 cap so $5k → 0.005.
    """
    latest_oi = ep.events[-1].open_interest if ep.events else 0
    if latest_oi <= 0:
        # No OI data: scale purely on premium magnitude (0–$1M = 0–1.0)
        return round(min(1.0, ep.total_premium / 1_000_000), 3)
    notional_oi = latest_oi * 100
    ratio = ep.total_premium / max(notional_oi, 1)
    return round(min(1.0, ratio), 3)


def compute_flow_score(ep: RepetitionEpisode) -> float:
    prem   = min(ep.total_premium / 10_000_000, 1.0)
    accel  = 0.15 if ep.is_accelerating else 0.0
    trades = min(ep.trade_count / 20, 0.20)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)


def build_composite(
    ep:          RepetitionEpisode,
    accumulator: RepetitionAccumulator,
) -> CompositeSignal:
    """Sync build — returns CompositeSignal WITHOUT swarm verdict.
    Use build_composite_async() to get full swarm output.
    """
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
    Builds composite signal then auto-runs the AI swarm.
    n_agents: overrides SWARM_N_AGENTS env var if provided.
    """
    # Use module-level run_ensemble (importable, patchable by tests)
    _run = run_ensemble
    if _run is None:
        try:
            from simulation.ensemble_runner import run_ensemble as _run_dyn
            _run = _run_dyn
        except Exception:
            _run = None

    sig = build_composite(ep, accumulator)

    if _run is None:
        return sig

    # Build flow event list for swarm context
    flow_events = [
        {
            "ticker":         ev.ticker if hasattr(ev, "ticker") else ep.ticker,
            "contract_type":  ep.contract_type,
            "strike":         getattr(ev, "strike", 0),
            "expiry":         getattr(ev, "expiry", ""),
            "premium":        getattr(ev, "premium", 0),
            "sentiment":      getattr(ev, "sentiment", "NEUTRAL"),
            "influence_tier": getattr(ev, "influence_tier", "RETAIL"),
            "is_golden_sweep": getattr(ev, "is_golden_sweep", False),
        }
        for ev in ep.events
    ]

    kwargs = {"ticker": ep.ticker, "flow_events": flow_events}
    if n_agents is not None:
        kwargs["n_agents"] = n_agents

    try:
        result = await _run(**kwargs)
        sig.swarm_direction  = result.direction
        sig.swarm_confidence = result.confidence
        sig.swarm_bull_votes = result.bull_votes
        sig.swarm_bear_votes = result.bear_votes
        sig.swarm_hold_votes = result.hold_votes
        sig.swarm_agents     = result.agents
    except Exception:
        # Swarm failure is non-fatal — composite score still valid
        pass

    return sig
