"""
composite_signal_engine.py — Apex S6

Composite formula overhaul:
  - Fake backtest influence removed: bt_s = 0.0, weight = 0.00.
  - New weight split: flow_score*0.55 + vwp_f*0.20 + prem_tier*0.15 + sector*0.10.
  - strong_sentiment gate: flow_s *= 0.80 when latest event is not strongly directional.
  - episode_influence_tier() uses episode total_premium, not event-level influence_tier.
  - composite_score_ceiling=0.90 documented and exposed in bus payload (sector_score reserved).
  - When sector_score is wired (S5 ladder context), composite_score_ceiling must be removed.

backtest_score field is preserved on CompositeSignal so callers do not break,
but its value is always 0.0 until S8 lands.
"""
from __future__ import annotations
from dataclasses import dataclass
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator


@dataclass
class CompositeSignal:
    ticker:                str
    recommendation:        str
    composite_score:       float
    flow_score:            float
    backtest_score:        float   # always 0.0 until S8; field preserved for callers
    volume_premium_factor: float
    premium_tier_score:    float
    reasoning:             str


# ---------------------------------------------------------------------------
# Influence tier — episode-level, not event-level
# ---------------------------------------------------------------------------

def episode_influence_tier(ep: RepetitionEpisode) -> str:
    """
    Map episode total_premium to an influence tier label.

    Uses episode premium, never the event-level influence_tier field, so the
    label reflects the full accumulated flow rather than any single print.
    """
    prem = ep.total_premium
    if prem >= 2_000_000:
        return "WHALE"
    if prem >= 500_000:
        return "INSTITUTIONAL"
    if prem >= 100_000:
        return "LARGE"
    return "RETAIL"


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

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


def premium_tier_score(ep: RepetitionEpisode) -> float:
    """
    Normalise episode total_premium to a [0, 1] score.

    Breakpoints are aligned with the Apex alert-level thresholds so the
    premium component of the composite score tracks the same bands used
    for alert-level classification.

      < $100k      -> 0.0  (WATCH band)
      $100k-$500k  -> 0.25 (ALERT band)
      $500k-$2M    -> 0.60 (STRONG_SIGNAL band)
      >= $2M       -> 1.0  (CONVICTION band)
    """
    prem = ep.total_premium
    if prem >= 2_000_000:
        return 1.0
    if prem >= 500_000:
        return 0.60
    if prem >= 100_000:
        return 0.25
    return 0.0


def compute_flow_score(ep: RepetitionEpisode) -> float:
    prem   = min(ep.total_premium / 10_000_000, 1.0)
    accel  = 0.15 if ep.is_accelerating else 0.0
    trades = min(ep.trade_count / 20, 0.20)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)


# ---------------------------------------------------------------------------
# Main composite builder
# ---------------------------------------------------------------------------

def build_composite(
    ep:          RepetitionEpisode,
    accumulator: RepetitionAccumulator,
) -> CompositeSignal:
    """
    Build a composite signal score from an episode.

    Weight split (S6):
        flow_score            * 0.55
        backtest_score        * 0.00   (zeroed until S8 real data lands)
        volume_premium_factor * 0.20
        premium_tier_score    * 0.15
        sector_score          * 0.10   (reserved — 0.0 until S5 ladder wired in)

    While sector_score == 0.0, the maximum achievable composite_score is 0.90.
    Do NOT redistribute the reserved 0.10 weight. Frontend consumers treat
    scores > 0.85 as effectively maximum conviction in the pre-ladder period.
    """
    latest = ep.events[-1]

    flow_s_raw = compute_flow_score(ep)

    # strong_sentiment gate: discount by 20% when direction is not strongly inferred
    strong = getattr(latest, "strong_sentiment", False)
    flow_s = round(flow_s_raw * (1.0 if strong else 0.80), 3)

    # Backtest score is zero until S8 real win-rate data lands.
    # Weight is explicitly 0.00 so fake seeded values cannot leak in.
    bt_s = 0.0

    vwp_f  = volume_weighted_premium_factor(ep)
    prem_t = premium_tier_score(ep)

    # sector_score reserved — activates when S5 ladder context is wired into S6.
    # NOTE: while sector_s == 0.0, maximum achievable composite_score is 0.90.
    # This is intentional. Do not redistribute the 0.10 weight.
    sector_s = 0.0

    comp = round(
        flow_s  * 0.55
        + bt_s  * 0.00
        + vwp_f * 0.20
        + prem_t * 0.15
        + sector_s * 0.10,
        3,
    )

    sentiment = getattr(latest, "sentiment", "BULLISH")
    if comp >= 0.65 and sentiment == "BULLISH":
        rec = "BUY"
    elif comp >= 0.65 and sentiment == "BEARISH":
        rec = "SELL"
    else:
        rec = "HOLD"

    tier = episode_influence_tier(ep)

    reasoning = (
        f"{ep.trade_count} {ep.contract_type} trades on {ep.ticker} "
        f"(${ep.total_premium:,.0f} total premium, {tier}). "
        f"Flow score {flow_s:.0%}"
        f"{' (strong)' if strong else ' (discounted — weak sentiment)'}, "
        f"backtest reserved (0%), "
        f"volume-premium factor {vwp_f:.0%}, "
        f"premium tier {prem_t:.0%}. "
        f"{'Accelerating flow detected. ' if ep.is_accelerating else ''}"
        f"Composite: {comp:.0%} -> {rec}. "
        f"[ceiling=0.90 until sector_score active]"
    )

    return CompositeSignal(
        ticker                = ep.ticker,
        recommendation        = rec,
        composite_score       = comp,
        flow_score            = flow_s,
        backtest_score        = bt_s,
        volume_premium_factor = vwp_f,
        premium_tier_score    = prem_t,
        reasoning             = reasoning,
    )
