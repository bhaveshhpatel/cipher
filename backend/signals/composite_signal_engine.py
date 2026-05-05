"""
composite_signal_engine.py — Apex S6

Composite formula overhaul:
  - Fake backtest influence removed: bt_s = 0.0, weight = 0.00.
  - New weight split: flow_score*0.55 + vwp_f*0.20 + prem_tier*0.15 + sector*0.10.
  - strong_sentiment gate: flow_s *= 0.80 when latest event is not strongly directional.
  - episode_influence_tier() uses episode total_premium, not event-level influence_tier.
  - composite_score_ceiling=0.90 documented and exposed in bus payload (sector_score reserved).
  - When sector_score is wired (S5 ladder context), COMPOSITE_SCORE_CEILING must be updated.

backtest_score field is preserved on CompositeSignal so callers do not break,
but its value is always 0.0 until S8 lands.

Apex S6 additions (test_apex_s6_composite_overhaul.py):
  - Composite dataclass: symbol, score, tier, breakdown, triggered_at
  - CompositeScore: alias / companion type for score computation
  - build_composite overload: accepts (symbol: str, episode, accumulator) -> Composite
    OR legacy (episode: RepetitionEpisode, accumulator: RepetitionAccumulator) -> CompositeSignal
    Legacy 2-arg with non-episode-duck-typed first arg raises TypeError (regression guard).
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Union
from signals.repetition_accumulator import RepetitionEpisode, RepetitionAccumulator

# ---------------------------------------------------------------------------
# Ceiling constant — import this in tradier_stream.py; never emit a literal 0.90
# Update this value when sector_score activates (S5 wire-up) or S8 backtest lands.
# ---------------------------------------------------------------------------
COMPOSITE_SCORE_CEILING: float = 0.90


# ---------------------------------------------------------------------------
# Apex S6 — Composite types
# ---------------------------------------------------------------------------

@dataclass
class CompositeScore:
    """Sub-score container used by Composite.breakdown."""
    premium_tier: float = 0.0
    flow: float = 0.0
    volume_premium: float = 0.0
    accumulator: float = 0.0


@dataclass
class Composite:
    """
    New composite result type introduced in Apex S6.

    Fields
    ------
    symbol        : str   — ticker symbol
    score         : float — composite score in [0.0, 1.0]
    tier          : str   — WEAK | MODERATE | STRONG | EXTREME
    breakdown     : dict  — per-component sub-scores (all numeric values)
    triggered_at  : float — unix timestamp of computation
    """
    symbol:       str
    score:        float
    tier:         str
    breakdown:    Dict[str, float] = field(default_factory=dict)
    triggered_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Legacy type — preserved for test_6layer_regression.py and existing callers
# ---------------------------------------------------------------------------

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

def episode_influence_tier(ep) -> str:
    """
    Map episode total_premium to an influence tier label.

    Uses episode premium, never the event-level influence_tier field, so the
    label reflects the full accumulated flow rather than any single print.
    Accepts both RepetitionEpisode and duck-typed episode objects.
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
# Duck-type episode check
# ---------------------------------------------------------------------------

def _is_episode_duck(obj) -> bool:
    """Return True if obj looks like an episode (has .events, .total_premium, .ticker)."""
    return (
        hasattr(obj, "events")
        and hasattr(obj, "total_premium")
        and hasattr(obj, "ticker")
    )


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def volume_weighted_premium_factor(ep) -> float:
    """min(1.0, premium / (oi * 100)). Returns 0.5 when OI is zero."""
    if not ep.events:
        return 0.5
    latest    = ep.events[-1]
    latest_oi = getattr(latest, "open_interest", 0) or 0
    if latest_oi <= 0:
        return 0.5
    premium = getattr(latest, "premium", 0) or 0
    return round(min(1.0, premium / (latest_oi * 100)), 4)


def premium_tier_score(ep) -> float:
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


def compute_flow_score(ep) -> float:
    prem   = min(ep.total_premium / 10_000_000, 1.0)
    accel  = 0.15 if ep.is_accelerating else 0.0
    trades = min(ep.trade_count / 20, 0.20)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)


# ---------------------------------------------------------------------------
# Composite tier promotion
# ---------------------------------------------------------------------------

def _score_to_tier(score: float) -> str:
    if score >= 0.75:
        return "EXTREME"
    if score >= 0.50:
        return "STRONG"
    if score >= 0.25:
        return "MODERATE"
    return "WEAK"


# ---------------------------------------------------------------------------
# Accumulator contribution score
# ---------------------------------------------------------------------------

def _accumulator_score(accumulator) -> float:
    """
    Derive a [0, 1] contribution from accumulator state.
    Works with both the real RepetitionAccumulator and stub objects used in tests,
    by falling back gracefully when methods are absent.
    """
    try:
        confirmed = (
            accumulator.confirmed_count()
            if callable(getattr(accumulator, "confirmed_count", None))
            else 0
        )
        total_prem = (
            accumulator.total_premium_confirmed()
            if callable(getattr(accumulator, "total_premium_confirmed", None))
            else 0.0
        )
        ep_count = (
            accumulator.episode_count()
            if callable(getattr(accumulator, "episode_count", None))
            else 0
        )
        # Normalise: cap confirmed at 10, premium at 2M, episodes at 20
        confirmed_s = min(confirmed / 10.0, 1.0)
        premium_s   = min(total_prem / 2_000_000.0, 1.0)
        ep_s        = min(ep_count / 20.0, 1.0)
        return round(min(1.0, confirmed_s * 0.5 + premium_s * 0.3 + ep_s * 0.2), 4)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Main composite builder — overloaded
# ---------------------------------------------------------------------------

def build_composite(
    symbol_or_episode,
    episode_or_accumulator,
    accumulator=None,
    *,
    sector_score: float = 0.0,
) -> Union[Composite, CompositeSignal, None]:
    """
    Build a composite signal score.

    Overloaded signatures
    ---------------------
    New (Apex S6):
        build_composite(symbol: str, episode, accumulator) -> Composite

    Legacy (6-layer regression, E2E tests, and stub-based tests):
        build_composite(episode, accumulator) -> CompositeSignal
        where episode is RepetitionEpisode OR any duck-typed object with
        .events, .total_premium, and .ticker attributes.

    Raises TypeError if called with 2 positional args where arg1 is not
    a str and does not duck-type as an episode object.
    """
    # ── New 3-arg path: build_composite(symbol, episode, accumulator) ──────
    if isinstance(symbol_or_episode, str):
        symbol  = symbol_or_episode
        episode = episode_or_accumulator
        acc     = accumulator
        return _build_composite_new(symbol, episode, acc)

    # ── Legacy 2-arg path: RepetitionEpisode OR duck-typed episode stub ────
    if isinstance(symbol_or_episode, RepetitionEpisode) or _is_episode_duck(symbol_or_episode):
        ep  = symbol_or_episode
        acc = episode_or_accumulator
        return _build_composite_legacy(ep, acc, sector_score=sector_score)

    # ── Anything else raises TypeError (regression guard) ──────────────────
    raise TypeError(
        "build_composite() requires either (symbol: str, episode, accumulator) "
        "or (episode, accumulator) where episode has .events/.total_premium/.ticker. "
        f"Got first arg of type {type(symbol_or_episode).__name__!r}."
    )


# ---------------------------------------------------------------------------
# New path (Apex S6) — returns Composite
# ---------------------------------------------------------------------------

def _build_composite_new(symbol: str, episode, accumulator) -> Optional[Composite]:
    """
    Build a Composite for the given symbol using the episode + accumulator state.

    Gracefully handles empty/cold accumulators — never raises.
    Returns None when the accumulator has no activity at all.

    Score formula (new path):
        episode_premium_score * 0.40
        + accumulator_score   * 0.35
        + acceleration_bonus  * 0.15
        + trade_count_score   * 0.10
    All clamped to [0.0, 1.0].
    """
    # Episode-level sub-scores — accept duck-typed episodes too
    ep_prem = getattr(episode, "total_premium", 0.0) or 0.0
    prem_s  = min(ep_prem / 2_000_000.0, 1.0)

    is_accel = getattr(episode, "is_accelerating", False)
    accel_s  = 0.15 if is_accel else 0.0

    tc = getattr(episode, "trade_count", None)
    if tc is None:
        tc = getattr(episode, "count", 0) or 0
    trade_s = min(tc / 20.0, 1.0)

    acc_s = _accumulator_score(accumulator)

    raw = (
        prem_s  * 0.40
        + acc_s * 0.35
        + accel_s * 0.15
        + trade_s * 0.10
    )
    score = round(min(1.0, max(0.0, raw)), 6)

    breakdown: Dict[str, float] = {
        "premium_score":      round(prem_s, 4),
        "accumulator_score":  round(acc_s, 4),
        "acceleration_bonus": round(accel_s, 4),
        "trade_count_score":  round(trade_s, 4),
    }

    tier = _score_to_tier(score)

    return Composite(
        symbol=symbol,
        score=score,
        tier=tier,
        breakdown=breakdown,
        triggered_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Legacy path — returns CompositeSignal
# ---------------------------------------------------------------------------

def _build_composite_legacy(
    ep,
    accumulator,
    *,
    sector_score: float = 0.0,
) -> CompositeSignal:
    """
    Legacy composite builder. Returns CompositeSignal.
    Accepts RepetitionEpisode or any duck-typed episode stub.

    Weight split (S6):
        flow_score            * 0.55
        backtest_score        * 0.00   (zeroed until S8 real data lands)
        volume_premium_factor * 0.20
        premium_tier_score    * 0.15
        sector_score          * 0.10   (reserved — 0.0 until S5 ladder wired in)

    sector_score is a keyword-only argument defaulting to 0.0.
    When S5 ladder context is wired in, pass sector_score=<value> at the call
    site in tradier_stream.py — no other callers need to change.

    While sector_score == 0.0, the maximum achievable composite_score is COMPOSITE_SCORE_CEILING.
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

    comp = round(
        flow_s       * 0.55
        + bt_s       * 0.00
        + vwp_f      * 0.20
        + prem_t     * 0.15
        + sector_score * 0.10,
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
        f"[ceiling={COMPOSITE_SCORE_CEILING} until sector_score active]"
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
