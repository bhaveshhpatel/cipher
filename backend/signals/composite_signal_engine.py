"""
Combines flow score + backtest score into a final composite signal.
"""
from dataclasses import dataclass
from parsers.options_flow_parser import OptionsFlowEvent
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator
from signals.backtest_validator import get_backtest_score

@dataclass
class CompositeSignal:
    ticker:          str
    recommendation:  str    # BUY | SELL | HOLD
    composite_score: float  # 0-1
    flow_score:      float
    backtest_score:  float
    reasoning:       str

def compute_flow_score(ep: RepetitionEpisode) -> float:
    """Score 0-1 based on premium, acceleration, tier."""
    prem    = min(ep.total_premium / 10_000_000, 1.0)  # cap at $10M
    accel   = 0.15 if ep.is_accelerating else 0.0
    trades  = min(ep.trade_count / 20, 0.20)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)

def build_composite(
    ep:             RepetitionEpisode,
    accumulator:    RepetitionAccumulator,
) -> CompositeSignal:
    latest  = ep.events[-1]
    flow_s  = compute_flow_score(ep)
    bt_s    = get_backtest_score(
        ep.ticker, ep.contract_type,
        latest.dte, latest.influence_tier,
    )
    comp    = round(flow_s * 0.6 + bt_s * 0.4, 3)

    # Recommendation logic
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
        f"Flow score {flow_s:.0%}, backtest win-rate {bt_s:.0%}. "
        f"{'Accelerating flow detected. ' if ep.is_accelerating else ''}"
        f"Composite: {comp:.0%} → {rec}."
    )

    return CompositeSignal(
        ticker          = ep.ticker,
        recommendation  = rec,
        composite_score = comp,
        flow_score      = flow_s,
        backtest_score  = bt_s,
        reasoning       = reasoning,
    )
