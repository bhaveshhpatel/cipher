"""
Combines flow score + backtest score + volume-weighted premium factor
into a final composite signal.

Phase 3 weight breakdown:
  flow_score              × 0.55  (raw premium + acceleration + trade count)
  backtest_score          × 0.35  (historical win-rate by ticker/type/DTE/tier)
  volume_premium_factor   × 0.10  (premium relative to open interest — conviction filter)
"""
from dataclasses import dataclass
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator
from signals.backtest_validator import get_backtest_score

@dataclass
class CompositeSignal:
    ticker:                 str
    recommendation:         str    # BUY | SELL | HOLD
    composite_score:        float  # 0-1
    flow_score:             float
    backtest_score:         float
    volume_premium_factor:  float  # Phase 3: new component
    reasoning:              str


def volume_weighted_premium_factor(ep: RepetitionEpisode) -> float:
    """
    Score 0-1 measuring premium conviction relative to open interest.

    Logic:
      - If OI is unavailable (0), fall back to a flat 0.5 neutral factor.
      - premium_per_contract = total_premium / (OI * 100)  [OI is in contracts]
      - Cap at 1.0. Values > 0.3 indicate meaningful accumulation vs existing OI.
    """
    latest_oi = ep.events[-1].open_interest if ep.events else 0
    if latest_oi <= 0:
        return 0.5  # neutral — no OI data available

    notional_oi = latest_oi * 100  # convert contracts → shares equivalent
    ratio = ep.total_premium / max(notional_oi, 1)
    return round(min(1.0, ratio), 3)


def compute_flow_score(ep: RepetitionEpisode) -> float:
    """Score 0-1 based on premium, acceleration, trade count."""
    prem   = min(ep.total_premium / 10_000_000, 1.0)  # cap at $10M
    accel  = 0.15 if ep.is_accelerating else 0.0
    trades = min(ep.trade_count / 20, 0.20)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)


def build_composite(
    ep:          RepetitionEpisode,
    accumulator: RepetitionAccumulator,
) -> CompositeSignal:
    latest   = ep.events[-1]
    flow_s   = compute_flow_score(ep)
    bt_s     = get_backtest_score(
        ep.ticker, ep.contract_type,
        latest.dte, latest.influence_tier,
    )
    vwp_f    = volume_weighted_premium_factor(ep)

    # Phase 3 weights: flow 55%, backtest 35%, volume-premium 10%
    comp     = round(flow_s * 0.55 + bt_s * 0.35 + vwp_f * 0.10, 3)

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
