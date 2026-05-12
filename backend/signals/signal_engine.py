"""
signal_engine.py — Pure computation helpers for REARCH-006 signal evaluation.

This module is intentionally free of I/O and async code.  It contains two
public functions that the signal orchestration layer (to be wired in a
subsequent REARCH-006 commit) calls synchronously before any DB write.

Public API
----------
compute_conviction_score(episode, cfg=None) -> int
    Returns an integer 0-5 — one point per Steamroom conviction dimension.
    See _score_* helpers below for per-dimension logic.

build_signal_row(episode, alert_level, direction, cfg=None) -> dict
    Assembles the exact insert dict that signal_store.persist_composite_signal
    / _build_row expects, keyed to the post-REARCH-010 signal_history column
    set (migration 024).

Column set reference (REARCH-010 / migration 024)
--------------------------------------------------
Kept  : ticker, recommendation, composite_score, reasoning, alert_level,
        direction, sentiment, premium, trade_type, contract_type,
        total_premium, trade_count, is_accelerating, signal_ts
Removed (DO NOT re-add without a migration):
        flow_score, volume_premium_factor, backtest_score,
        swarm_direction, swarm_confidence, swarm_agents,
        swarm_bull_votes, swarm_bear_votes, swarm_hold_votes,
        influence_tier, is_golden_sweep
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("signal_engine")

# ---------------------------------------------------------------------------
# Steamroom hard-coded defaults (used when no SignalConfig is supplied)
# ---------------------------------------------------------------------------

# Minimum notional premium per notional_tier for a point to be awarded.
# Mirrors the Steamroom strategy floors: Tier-1 names demand bigger flow.
_DEFAULT_PREMIUM_FLOOR_BY_TIER: dict[str, int] = {
    "large":  500_000,
    "mid":    250_000,
    "small":  100_000,
    "nano":    50_000,
}
_DEFAULT_PREMIUM_FLOOR_FALLBACK = 100_000  # used when notional_tier is absent

# Ask-side execution: minimum fraction of trades that must print on the ask.
_DEFAULT_ASK_SIDE_PCT_FLOOR: float = 0.60  # 60 %

# Repetition: minimum trade count within the episode.
_DEFAULT_MIN_TRADE_COUNT: int = 3

# DTE buckets that indicate quality conviction flow.
_QUALITY_DTE_BUCKETS: frozenset[str] = frozenset({"near", "mid"})


# ---------------------------------------------------------------------------
# Dimension scorers  (private — one per Steamroom gate)
# ---------------------------------------------------------------------------

def _score_premium(episode: dict, cfg: Optional[Any]) -> bool:
    """Dimension 1 — Premium threshold.

    Awards a point when the episode's total_premium clears the tier-aware
    floor drawn from the SignalConfig (if supplied) or the hard-coded
    Steamroom defaults.

    Config knobs consumed (when cfg is not None):
        cfg.golden_sweep_premium          — base floor (int)
        cfg.tier_multipliers              — dict[notional_tier, float]
        cfg.get_effective_premium_threshold(notional_tier) — preferred path
    """
    total_premium = episode.get("total_premium") or 0
    notional_tier = (episode.get("notional_tier") or "").lower()

    if cfg is not None and hasattr(cfg, "get_effective_premium_threshold"):
        try:
            floor = cfg.get_effective_premium_threshold(notional_tier)
            return total_premium >= floor
        except Exception as exc:
            log.warning(
                "[signal_engine] get_effective_premium_threshold raised %s — "
                "falling back to default floors",
                exc,
            )

    # Hard-coded fallback
    floor = _DEFAULT_PREMIUM_FLOOR_BY_TIER.get(
        notional_tier, _DEFAULT_PREMIUM_FLOOR_FALLBACK
    )
    return total_premium >= floor


def _score_ask_side(episode: dict, cfg: Optional[Any]) -> bool:
    """Dimension 2 — Ask-side execution.

    Awards a point when ask_side_pct (fraction of trades that printed on the
    ask) meets or exceeds the configured floor.

    Config knobs: cfg.ask_side_pct_floor (float, 0-1)
    Also gated by cfg.require_ask_side (bool) — if False the point is always
    awarded (the dimension is disabled at the config level).
    """
    # If config explicitly disables this gate, treat as passing.
    if cfg is not None and hasattr(cfg, "require_ask_side"):
        if not cfg.require_ask_side:
            return True

    ask_side_pct = episode.get("ask_side_pct") or 0.0

    if cfg is not None and hasattr(cfg, "ask_side_pct_floor"):
        floor = cfg.ask_side_pct_floor or _DEFAULT_ASK_SIDE_PCT_FLOOR
    else:
        floor = _DEFAULT_ASK_SIDE_PCT_FLOOR

    return ask_side_pct >= floor


def _score_vol_oi(episode: dict, cfg: Optional[Any]) -> bool:
    """Dimension 3 — Volume > Open Interest.

    Awards a point when at least one constituent flow event has
    vol_oi_signal=True.  The episode dict is expected to carry this as a
    pre-computed aggregate boolean (added in REARCH-003/004 enrichment).

    Config knob: cfg.require_vol_gt_oi (bool) — if False, always passes.
    """
    if cfg is not None and hasattr(cfg, "require_vol_gt_oi"):
        if not cfg.require_vol_gt_oi:
            return True

    return bool(episode.get("vol_oi_signal", False))


def _score_dte(episode: dict, cfg: Optional[Any]) -> bool:
    """Dimension 4 — DTE quality bucket.

    Awards a point when dte_bucket indicates near or mid-term expiry,
    which aligns with Steamroom's conviction-flow window.

    Config knobs: cfg.min_dte, cfg.max_dte (ints) — these inform how the
    upstream enricher classifies dte_bucket; here we simply read the bucket
    label already stamped on the episode by REARCH-004.
    """
    dte_bucket = (episode.get("dte_bucket") or "").lower()
    return dte_bucket in _QUALITY_DTE_BUCKETS


def _score_repetition(episode: dict, cfg: Optional[Any]) -> bool:
    """Dimension 5 — Repetition / clustering.

    Awards a point when the episode contains enough distinct trades to
    confirm patterned accumulation rather than a one-off print.

    Config knob: cfg.min_trade_count (int)
    """
    trade_count = episode.get("trade_count") or 0

    if cfg is not None and hasattr(cfg, "min_trade_count"):
        min_count = cfg.min_trade_count or _DEFAULT_MIN_TRADE_COUNT
    else:
        min_count = _DEFAULT_MIN_TRADE_COUNT

    return trade_count >= min_count


# ---------------------------------------------------------------------------
# Public: compute_conviction_score
# ---------------------------------------------------------------------------

def compute_conviction_score(
    episode: dict,
    cfg: Optional[Any] = None,
) -> int:
    """Return an integer conviction score 0-5 for *episode*.

    One point is awarded per Steamroom dimension:
        1. Premium threshold  (total_premium vs tier-aware floor)
        2. Ask-side execution (ask_side_pct >= floor)
        3. Vol > OI           (vol_oi_signal=True on at least one event)
        4. DTE quality        (dte_bucket in {near, mid})
        5. Repetition         (trade_count >= min_trade_count)

    Parameters
    ----------
    episode : dict
        Enriched episode dict carrying the REARCH-003/004 columns:
        ask_side_pct, ask_side_count, vol_oi_signal, dte_bucket,
        notional_tier, trade_count, total_premium.
    cfg : SignalConfig | None
        Optional live config object from SignalConfigStore.  When None the
        function uses Steamroom hard-coded defaults, making it fully
        unit-testable without a live DB or cache.

    Returns
    -------
    int
        Score in [0, 5].  5 = all five Steamroom dimensions confirmed.
        GOLDEN alert level requires score == 5 by convention.
    """
    scorers = [
        _score_premium,
        _score_ask_side,
        _score_vol_oi,
        _score_dte,
        _score_repetition,
    ]
    score = sum(1 for fn in scorers if fn(episode, cfg))
    log.debug(
        "[signal_engine] conviction_score=%d ticker=%s trade_count=%s "
        "dte_bucket=%s notional_tier=%s vol_oi=%s ask_side_pct=%s",
        score,
        episode.get("ticker"),
        episode.get("trade_count"),
        episode.get("dte_bucket"),
        episode.get("notional_tier"),
        episode.get("vol_oi_signal"),
        episode.get("ask_side_pct"),
    )
    return score


# ---------------------------------------------------------------------------
# Alert-level derivation  (REARCH-010 vocab: WATCH|NOTEWORTHY|BLOCK|GOLDEN)
# ---------------------------------------------------------------------------

def _alert_level_from_score(conviction_score: int) -> str:
    """Map a 0-5 conviction score to the REARCH alert level vocab.

    GOLDEN requires all 5 dimensions — it cannot be reached by score alone
    without explicit caller confirmation (matches signal_store._build_row
    comment: 'GOLDEN cannot be score-derived from composite_score alone').
    """
    if conviction_score == 5:
        return "GOLDEN"
    if conviction_score >= 4:
        return "BLOCK"
    if conviction_score >= 2:
        return "NOTEWORTHY"
    return "WATCH"


# ---------------------------------------------------------------------------
# Public: build_signal_row
# ---------------------------------------------------------------------------

def build_signal_row(
    episode: dict,
    alert_level: str,
    direction: str,
    cfg: Optional[Any] = None,
) -> dict:
    """Assemble the insert dict for signal_store.persist_composite_signal.

    The returned dict is keyed to the post-REARCH-010 signal_history column
    set (migration 024).  Callers should pass the dict as the *sig* argument
    to persist_composite_signal, with *episode* passed separately as *ep*
    if further enrichment is needed — or pass the merged result directly to
    save_signal().

    Parameters
    ----------
    episode : dict
        Enriched episode dict (REARCH-003/004 columns present).
    alert_level : str
        REARCH vocab: WATCH | NOTEWORTHY | BLOCK | GOLDEN.
        Validated upstream by _normalise_alert_level in signal_store.
    direction : str
        REARCH vocab: BULLISH | BEARISH | NEUTRAL.
        Validated upstream by _normalise_direction in signal_store.
    cfg : SignalConfig | None
        Used to compute the conviction score that drives composite_score.

    Returns
    -------
    dict
        Ready-to-insert row.  Keys exactly mirror _build_row() in
        signal_store.py — do not add keys that no longer exist post-migration.

    Column set (migration 024 — REARCH-010 purge)
    ----------------------------------------------
    Included   : ticker, recommendation, composite_score, reasoning,
                 alert_level, direction, sentiment, premium, trade_type,
                 contract_type, total_premium, trade_count, is_accelerating,
                 signal_ts
    Intentionally absent (dropped in migration 024):
                 flow_score, volume_premium_factor, backtest_score,
                 swarm_direction, swarm_confidence, swarm_agents,
                 swarm_bull_votes, swarm_bear_votes, swarm_hold_votes,
                 influence_tier, is_golden_sweep
    """
    conviction_score = compute_conviction_score(episode, cfg)
    # Normalise composite_score to [0.0, 1.0] — signal_store expects a float.
    composite_score = round(conviction_score / 5.0, 2)

    ticker       = episode.get("ticker") or episode.get("symbol")
    trade_type   = (episode.get("trade_type") or "").upper() or "SINGLE"
    contract_type = episode.get("contract_type") or episode.get("option_type")
    total_premium = episode.get("total_premium") or 0
    trade_count   = episode.get("trade_count") or 0
    signal_ts     = episode.get("timestamp") or episode.get("signal_ts")
    is_accelerating = bool(episode.get("is_accelerating", False))

    # Derive sentiment from direction (mirrors signal_store._build_row logic).
    dir_upper = (direction or "").upper()
    ctype_upper = (contract_type or "").upper()
    if "BULLISH" in dir_upper or ctype_upper == "CALL":
        sentiment = "BULLISH"
    elif "BEARISH" in dir_upper or ctype_upper == "PUT":
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    # Build a human-readable recommendation string.
    recommendation = (
        f"{alert_level} {direction} | score={conviction_score}/5 | "
        f"premium=${total_premium:,.0f} | trades={trade_count}"
    )

    # Reasoning encodes which dimensions fired for observability.
    dim_labels = [
        ("premium",    _score_premium(episode, cfg)),
        ("ask_side",   _score_ask_side(episode, cfg)),
        ("vol_oi",     _score_vol_oi(episode, cfg)),
        ("dte",        _score_dte(episode, cfg)),
        ("repetition", _score_repetition(episode, cfg)),
    ]
    fired   = [label for label, passed in dim_labels if passed]
    missing = [label for label, passed in dim_labels if not passed]
    reasoning = (
        f"Steamroom conviction {conviction_score}/5. "
        f"Passed: {', '.join(fired) if fired else 'none'}. "
        f"Missing: {', '.join(missing) if missing else 'none'}."
    )

    return {
        "ticker":          ticker,
        "recommendation":  recommendation,
        "composite_score": composite_score,
        "reasoning":       reasoning,
        "alert_level":     alert_level,
        "direction":       direction,
        "sentiment":       sentiment,
        "premium":         total_premium,
        "trade_type":      trade_type,
        "contract_type":   contract_type or None,
        "total_premium":   total_premium,
        "trade_count":     trade_count,
        "is_accelerating": is_accelerating,
        "signal_ts":       signal_ts,
    }
