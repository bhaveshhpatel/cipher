"""
composite_signal_engine.py — Apex S0

Swarm / async layer removed. build_composite_async, run_ensemble import,
and all swarm fields on CompositeSignal are gone. Only the synchronous
hot-path (build_composite) survives.

vwpf formula: min(1.0, premium / (oi * 100))
"""
from __future__ import annotations
from dataclasses import dataclass
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator
from signals.backtest_validator import get_backtest_score


@dataclass
class CompositeSignal:
    ticker:                str
    recommendation:        str
    composite_score:       float
    flow_score:            float
    backtest_score:        float
    volume_premium_factor: float
    reasoning:             str


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
