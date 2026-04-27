"""
signals/signal_gate.py — Apex Layer 1: Hard Rejection Gates

Sits between L4 (DedupCache) and L5 (RepetitionAccumulator).
All gates are deterministic, sub-millisecond, zero I/O.

Gate execution order (fail-fast — cheapest checks first):
  1. Sweep-only gate       — trade_type must be SWEEP
  2. Spread gate           — (ask - bid) / mid <= MAX_SPREAD_PCT
  3. Min premium gate      — individual trade premium >= MIN_TRADE_PREMIUM
  4. Volume > OI gate      — daily volume > open_interest (new-bet filter)
  5. Aggression gate       — proportional fill-distance penalty (soft default)
                             calls -> AT_ASK or ABOVE_ASK (else penalty)
                             puts  -> AT_BID or BELOW_BID (else penalty)

Aggression gate penalty model:
  Penalty = clamp(fill_distance_pct, 0.05, MAX_AGGRESSION_PENALTY)
  where fill_distance_pct for a CALL = (fill_price - ask) / ask  (negative
  means filled below ask — less aggressive). Mirrored for PUTs.
  If bid/ask/fill_price are unavailable, falls back to FLAT_AGGRESSION_PENALTY.

  Set APEX_AGGRESSION_HARD_REJECT=true to drop non-aggressive fills entirely
  instead of penalising.  Set APEX_MAX_AGGRESSION_PENALTY to override the 0.40
  cap (float, 0-1).

Stats counter:
  signal_gate.stats() returns a dict of per-gate rejection counts
  for the /health endpoint and Railway observability.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration (all overridable via env vars)
# ---------------------------------------------------------------------------

MAX_SPREAD_PCT: float        = float(os.getenv("APEX_MAX_SPREAD_PCT",          "0.15"))   # 15%
MIN_TRADE_PREMIUM: float     = float(os.getenv("APEX_MIN_TRADE_PREMIUM",       "5000"))  # $5K
MAX_AGGRESSION_PENALTY: float = float(os.getenv("APEX_MAX_AGGRESSION_PENALTY", "0.40"))  # 40% cap
FLAT_AGGRESSION_PENALTY: float = float(os.getenv("APEX_FLAT_AGGRESSION_PENALTY", "0.25")) # fallback
AGGRESSION_HARD_REJECT: bool = os.getenv("APEX_AGGRESSION_HARD_REJECT", "false").lower() == "true"

# Runtime-mutable flag — updated by /api/apex/gate-config PATCH without restart
_aggression_hard_reject_override: Optional[bool] = None

# bid_ask classes that count as aggressive for calls
_CALL_AGGRESSIVE = frozenset({"AT_ASK", "ABOVE_ASK"})
# bid_ask classes that count as aggressive for puts
_PUT_AGGRESSIVE  = frozenset({"AT_BID", "BELOW_BID"})


def get_aggression_hard_reject() -> bool:
    """Returns effective hard-reject flag (runtime override takes precedence)."""
    if _aggression_hard_reject_override is not None:
        return _aggression_hard_reject_override
    return AGGRESSION_HARD_REJECT


def set_aggression_hard_reject(value: bool) -> None:
    """Set runtime override for aggression hard-reject (no restart needed)."""
    global _aggression_hard_reject_override
    _aggression_hard_reject_override = value


def reset_aggression_override() -> None:
    """Reset override to None so env-var default is used again."""
    global _aggression_hard_reject_override
    _aggression_hard_reject_override = None


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class GateVerdict(str, Enum):
    PASS        = "PASS"         # event clears all gates
    HARD_REJECT = "HARD_REJECT"  # event is dropped immediately
    SOFT_REJECT = "SOFT_REJECT"  # event passes but conviction is penalised


@dataclass
class GateResult:
    verdict:       GateVerdict
    failed_gate:   Optional[str] = None   # name of the gate that rejected
    reason:        Optional[str] = None   # human-readable reason
    score_penalty: float         = 0.0    # applied to conviction_score on SOFT_REJECT

    @property
    def passed(self) -> bool:
        return self.verdict == GateVerdict.PASS

    @property
    def hard_rejected(self) -> bool:
        return self.verdict == GateVerdict.HARD_REJECT

    @property
    def soft_rejected(self) -> bool:
        return self.verdict == GateVerdict.SOFT_REJECT


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class _GateStats:
    total_seen:           int = 0
    hard_rejected:        int = 0
    soft_rejected:        int = 0
    passed:               int = 0
    rejected_sweep_only:  int = 0
    rejected_spread:      int = 0
    rejected_min_premium: int = 0
    rejected_vol_oi:      int = 0
    flagged_aggression:   int = 0


_stats = _GateStats()


def stats() -> dict:
    """Returns current gate rejection counters — safe to call from /health."""
    return {
        "gate_total_seen":           _stats.total_seen,
        "gate_hard_rejected":        _stats.hard_rejected,
        "gate_soft_rejected":        _stats.soft_rejected,
        "gate_passed":               _stats.passed,
        "gate_rejected_sweep_only":  _stats.rejected_sweep_only,
        "gate_rejected_spread":      _stats.rejected_spread,
        "gate_rejected_min_premium": _stats.rejected_min_premium,
        "gate_rejected_vol_oi":      _stats.rejected_vol_oi,
        "gate_flagged_aggression":   _stats.flagged_aggression,
        "aggression_hard_reject":    get_aggression_hard_reject(),
    }


def reset_stats() -> None:
    """Reset all counters — used in tests."""
    global _stats
    _stats = _GateStats()


# ---------------------------------------------------------------------------
# Penalty helper
# ---------------------------------------------------------------------------

def _compute_aggression_penalty(ev, ctype: str) -> float:
    """
    Compute proportional fill-distance penalty.

    For CALLs: how far below the ask did the fill land?
      distance = (ask - fill) / ask   [positive = filled below ask = less aggressive]
    For PUTs: how far above the bid?
      distance = (fill - bid) / bid   [positive = filled above bid = less aggressive]

    Result is clamped to [FLAT_AGGRESSION_PENALTY, MAX_AGGRESSION_PENALTY].
    Falls back to FLAT_AGGRESSION_PENALTY when price data unavailable.
    """
    bid  = float(getattr(ev, "bid",        0) or 0)
    ask  = float(getattr(ev, "ask",        0) or 0)
    fill = float(getattr(ev, "fill_price", 0) or 0)

    if fill <= 0 or (ctype == "CALL" and ask <= 0) or (ctype == "PUT" and bid <= 0):
        return FLAT_AGGRESSION_PENALTY

    if ctype == "CALL":
        distance = (ask - fill) / ask
    else:
        distance = (fill - bid) / bid

    # distance <= 0 means filled AT or above ask (calls) / AT or below bid (puts)
    # that shouldn't reach here (those are aggressive), but guard anyway
    if distance <= 0:
        return FLAT_AGGRESSION_PENALTY

    return min(max(distance, FLAT_AGGRESSION_PENALTY), MAX_AGGRESSION_PENALTY)


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

def _gate_sweep_only(ev) -> Optional[GateResult]:
    """Gate 1: Only SWEEP trades proceed to the accumulator."""
    trade_type = getattr(ev, "trade_type", "") or ""
    if trade_type.upper() != "SWEEP":
        _stats.rejected_sweep_only += 1
        return GateResult(
            verdict=GateVerdict.HARD_REJECT,
            failed_gate="sweep_only",
            reason=f"trade_type={trade_type!r} is not SWEEP",
        )
    return None


def _gate_spread(ev) -> Optional[GateResult]:
    """Gate 2: (ask - bid) / mid must be <= MAX_SPREAD_PCT."""
    bid = float(getattr(ev, "bid", 0) or 0)
    ask = float(getattr(ev, "ask", 0) or 0)

    if getattr(ev, "is_synthetic_quote", False):
        return None

    if bid <= 0 or ask <= 0:
        return None

    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None

    spread_pct = (ask - bid) / mid
    if spread_pct > MAX_SPREAD_PCT:
        _stats.rejected_spread += 1
        return GateResult(
            verdict=GateVerdict.HARD_REJECT,
            failed_gate="spread",
            reason=f"spread {spread_pct:.1%} > {MAX_SPREAD_PCT:.1%} max",
        )
    return None


def _gate_min_premium(ev) -> Optional[GateResult]:
    """Gate 3: Individual trade premium must be >= MIN_TRADE_PREMIUM."""
    premium = float(getattr(ev, "premium", 0) or 0)
    if premium < MIN_TRADE_PREMIUM:
        _stats.rejected_min_premium += 1
        return GateResult(
            verdict=GateVerdict.HARD_REJECT,
            failed_gate="min_premium",
            reason=f"premium ${premium:,.0f} < ${MIN_TRADE_PREMIUM:,.0f} minimum",
        )
    return None


def _gate_volume_oi(ev) -> Optional[GateResult]:
    """
    Gate 4: Daily volume must exceed open interest.
    Skip when open_interest == 0 (data not available).
    """
    oi     = int(getattr(ev, "open_interest", 0) or 0)
    if oi == 0:
        return None

    volume = int(getattr(ev, "daily_volume", None) or getattr(ev, "size", 0) or 0)
    if volume == 0:
        return None

    if volume <= oi:
        _stats.rejected_vol_oi += 1
        return GateResult(
            verdict=GateVerdict.HARD_REJECT,
            failed_gate="volume_oi",
            reason=f"volume {volume} <= OI {oi} (not a new bet)",
        )
    return None


def _gate_aggression(ev) -> Optional[GateResult]:
    """
    Gate 5: Aggression check with proportional fill-distance penalty.

    Calls must fill AT_ASK or ABOVE_ASK.
    Puts  must fill AT_BID  or BELOW_BID.

    Non-aggressive fills receive a proportional score_penalty based on how
    far the fill deviated from the aggressive threshold, capped at
    MAX_AGGRESSION_PENALTY (default 0.40).  Falls back to
    FLAT_AGGRESSION_PENALTY (0.25) when price data is absent.

    Set APEX_AGGRESSION_HARD_REJECT=true (or toggle via /api/apex/gate-config)
    to hard-reject instead of penalise.
    """
    ba_class = (getattr(ev, "bid_ask_class",  "") or "").upper()
    ctype    = (getattr(ev, "contract_type",  "") or "").upper()

    if not ba_class or not ctype:
        return None

    is_aggressive = (
        (ctype == "CALL" and ba_class in _CALL_AGGRESSIVE) or
        (ctype == "PUT"  and ba_class in _PUT_AGGRESSIVE)
    )

    if not is_aggressive:
        _stats.flagged_aggression += 1
        penalty = _compute_aggression_penalty(ev, ctype)
        verdict = GateVerdict.HARD_REJECT if get_aggression_hard_reject() else GateVerdict.SOFT_REJECT
        return GateResult(
            verdict=verdict,
            failed_gate="aggression",
            reason=f"{ctype} filled at {ba_class!r} — not aggressive (penalty={penalty:.2f})",
            score_penalty=penalty,
        )
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_GATES = [
    _gate_sweep_only,
    _gate_spread,
    _gate_min_premium,
    _gate_volume_oi,
    _gate_aggression,
]


def check(ev) -> GateResult:
    """
    Run all gates against the given OptionsFlowEvent.

    Returns GateResult with:
      .passed        == True   → forward to RepetitionAccumulator
      .hard_rejected == True   → drop event entirely
      .soft_rejected == True   → forward but apply .score_penalty to conviction_score

    Call site in tradier_stream._process_trade():

        from signals.signal_gate import check as gate_check

        result = gate_check(ev)
        if result.hard_rejected:
            return
        if result.soft_rejected:
            ev.conviction_score = round(
                ev.conviction_score * (1 - result.score_penalty), 3
            )
        await accumulator.ingest(ev)
    """
    _stats.total_seen += 1

    for gate_fn in _GATES:
        result = gate_fn(ev)
        if result is not None:
            if result.hard_rejected:
                _stats.hard_rejected += 1
            else:
                _stats.soft_rejected += 1
            return result

    _stats.passed += 1
    return GateResult(verdict=GateVerdict.PASS)
