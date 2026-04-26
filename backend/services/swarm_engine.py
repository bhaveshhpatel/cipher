"""
services/swarm_engine.py — Swarm scoring engine.

Evaluates an options flow event through a lightweight multi-agent
scoring swarm and returns a scored result dict.

Public API:
  run_swarm(flow) → dict   (score, conviction, tier, agents)
  evaluate(flow)  → dict   (alias for run_swarm)
  AGENTS          list[str]

Design:
  - Pure in-process logic — no DB, no HTTP.
  - Works with any object that has optional attributes matching SymbolQuote
    / flow event fields: composite_score, flow_score, backtest_score,
    premium, total_premium, volume_premium_factor, is_accelerating,
    influence_tier, direction, contract_type, trade_count.
  - Missing fields default to sensible values so the engine is tolerant
    of minimal test stubs.
  - Score is always in [0.0, 1.0].
  - Higher premium + higher composite_score = higher swarm score.
    (High-conviction flows score above low-conviction flows.)
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("swarm_engine")

# Named agents in the swarm — exposed for introspection / tests.
AGENTS: list[str] = [
    "momentum",
    "premium_weight",
    "backtest",
    "influence",
    "acceleration",
]
_agents = AGENTS  # lowercase alias

# Premium thresholds used for normalisation
_PREMIUM_SCALE = 1_000_000.0   # $1M = max premium contribution


def _safe_float(obj: Any, attr: str, default: float = 0.0) -> float:
    val = getattr(obj, attr, None)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_bool(obj: Any, attr: str, default: bool = False) -> bool:
    val = getattr(obj, attr, None)
    if val is None:
        return default
    return bool(val)


def _safe_str(obj: Any, attr: str, default: str = "") -> str:
    val = getattr(obj, attr, None)
    if val is None:
        return default
    return str(val)


def _momentum_score(flow: Any) -> float:
    """Base signal from composite and flow scores."""
    composite = _safe_float(flow, "composite_score", 0.5)
    flow_sc   = _safe_float(flow, "flow_score",      0.5)
    return (composite * 0.6 + flow_sc * 0.4)


def _premium_weight(flow: Any) -> float:
    """Normalised premium contribution — larger premium = higher weight."""
    premium       = _safe_float(flow, "premium",       0.0)
    total_premium = _safe_float(flow, "total_premium", premium)
    best = max(premium, total_premium)
    return min(best / _PREMIUM_SCALE, 1.0)


def _backtest_score(flow: Any) -> float:
    return _safe_float(flow, "backtest_score", 0.5)


def _influence_score(flow: Any) -> float:
    tier = _safe_str(flow, "influence_tier", "").upper()
    return {"WHALE": 1.0, "INSTITUTION": 0.85, "RETAIL": 0.4}.get(tier, 0.6)


def _acceleration_bonus(flow: Any) -> float:
    return 0.05 if _safe_bool(flow, "is_accelerating") else 0.0


async def run_swarm(flow: Any) -> dict:
    """
    Run the swarm scoring pipeline for a single flow event.

    Returns a dict with keys:
      score           float  [0, 1]  — final weighted swarm score
      conviction      str           — HIGH / MEDIUM / LOW
      agents          dict          — per-agent scores for transparency
      composite_score float         — echoed from input (or computed)
      direction       str           — echoed from input
    """
    mom    = _momentum_score(flow)
    prem   = _premium_weight(flow)
    bt     = _backtest_score(flow)
    infl   = _influence_score(flow)
    accel  = _acceleration_bonus(flow)

    # Weighted combination
    raw = (
        mom   * 0.35
        + prem  * 0.25
        + bt    * 0.20
        + infl  * 0.15
        + accel
    )
    score = min(max(raw, 0.0), 1.0)

    if score >= 0.70:
        conviction = "HIGH"
    elif score >= 0.45:
        conviction = "MEDIUM"
    else:
        conviction = "LOW"

    log.debug(
        "[swarm_engine] symbol=%s score=%.3f conviction=%s",
        getattr(flow, "symbol", "?"), score, conviction,
    )

    return {
        "score":           score,
        "composite_score": _safe_float(flow, "composite_score", score),
        "conviction":      conviction,
        "direction":       _safe_str(flow, "direction", "unknown"),
        "agents": {
            "momentum":       mom,
            "premium_weight": prem,
            "backtest":       bt,
            "influence":      infl,
            "acceleration":   accel,
        },
    }


# Alias so tests can call either name
evaluate = run_swarm
