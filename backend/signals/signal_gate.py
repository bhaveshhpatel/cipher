"""
signals/signal_gate.py — Apex Phase 1: Hard Rejection Gates

Five deterministic gates that run AFTER Layer 4 dedup, BEFORE the
RepetitionAccumulator. All gates are sub-millisecond (no I/O, no AI).
Any failure immediately drops the event — the accumulator never sees it.

Gates (in order):
  1. Sweep-only      : trade_type must be SWEEP
  2. Aggression      : calls AT_ASK/ABOVE_ASK; puts AT_BID/BELOW_BID
  3. Volume > OI     : open_interest > 0 AND size > open_interest (new-bet filter)
  4. Spread          : (ask - bid) / mid <= MAX_SPREAD_RATIO (default 0.15)
  5. Min premium     : fill_price * size * 100 >= MIN_TRADE_PREMIUM (default $5K)

Non-sweep events are NOT immediately discarded — they are tagged with
is_apex_non_sweep=True and returned as REJECTED so the caller can feed
them to a counter-flow accumulator if desired (Apex Layer 0 spec).

Usage:
    from signals.signal_gate import apex_gate, GateVerdict

    result = apex_gate.check(ev)
    if result.verdict == GateVerdict.REJECTED:
        _stats["hard_rejected"] += 1
        return  # drop — never reaches accumulator
    # ... proceed to RepetitionAccumulator

Configuration (env vars, all optional):
    APEX_MODE              = "true"  # must be true for gates to fire
    APEX_MIN_PREMIUM_GATE  = 5000    # minimum individual trade premium in USD
    APEX_MAX_SPREAD_RATIO  = 0.15    # maximum (ask-bid)/mid ratio
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_APEX_MODE         = os.getenv("APEX_MODE", "false").lower() == "true"
_MIN_PREMIUM       = float(os.getenv("APEX_MIN_PREMIUM_GATE", "5000"))
_MAX_SPREAD_RATIO  = float(os.getenv("APEX_MAX_SPREAD_RATIO", "0.15"))

# Aggression: classes that pass for calls and puts respectively
_CALL_PASS_CLASSES = frozenset({"AT_ASK", "ABOVE_ASK"})
_PUT_PASS_CLASSES  = frozenset({"AT_BID", "BELOW_BID"})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
class GateVerdict(str, Enum):
    PASSED   = "PASSED"
    REJECTED = "REJECTED"
    BYPASSED = "BYPASSED"  # APEX_MODE=false — gate not active


@dataclass
class HardGateResult:
    verdict:      GateVerdict
    rejected_by:  Optional[str] = None   # gate name that fired
    reason:       Optional[str] = None   # human-readable detail
    is_non_sweep: bool          = False  # True for counter-flow tagging


_PASS   = HardGateResult(verdict=GateVerdict.PASSED)
_BYPASS = HardGateResult(verdict=GateVerdict.BYPASSED)


# ---------------------------------------------------------------------------
# Gate engine
# ---------------------------------------------------------------------------
class SignalGate:
    """
    Stateless hard-gate evaluator.

    All gates are pure functions of the OptionsFlowEvent — no state,
    no I/O, no async. Safe to call from any concurrent worker.
    """

    def __init__(
        self,
        apex_mode:        bool  = _APEX_MODE,
        min_premium:      float = _MIN_PREMIUM,
        max_spread_ratio: float = _MAX_SPREAD_RATIO,
    ):
        self._active           = apex_mode
        self._min_premium      = min_premium
        self._max_spread_ratio = max_spread_ratio

        # Observability counters
        self._stats: dict[str, int] = {
            "gate_evaluated":  0,
            "gate_passed":     0,
            "gate_bypassed":   0,
            "rejected_sweep":  0,   # Gate 1
            "rejected_aggr":   0,   # Gate 2
            "rejected_vol_oi": 0,   # Gate 3
            "rejected_spread": 0,   # Gate 4
            "rejected_prem":   0,   # Gate 5
            "total_rejected":  0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, ev) -> HardGateResult:  # ev: OptionsFlowEvent
        """
        Evaluate all hard gates against the event.
        Returns HardGateResult with verdict and rejection detail.
        """
        self._stats["gate_evaluated"] += 1

        if not self._active:
            self._stats["gate_bypassed"] += 1
            return _BYPASS

        # Gate 1 — Sweep only
        result = self._gate_sweep(ev)
        if result is not None:
            self._stats["rejected_sweep"] += 1
            self._stats["total_rejected"] += 1
            return result

        # Gate 2 — Aggression
        result = self._gate_aggression(ev)
        if result is not None:
            self._stats["rejected_aggr"] += 1
            self._stats["total_rejected"] += 1
            return result

        # Gate 3 — Volume > OI
        result = self._gate_volume_oi(ev)
        if result is not None:
            self._stats["rejected_vol_oi"] += 1
            self._stats["total_rejected"] += 1
            return result

        # Gate 4 — Spread
        result = self._gate_spread(ev)
        if result is not None:
            self._stats["rejected_spread"] += 1
            self._stats["total_rejected"] += 1
            return result

        # Gate 5 — Min premium
        result = self._gate_premium(ev)
        if result is not None:
            self._stats["rejected_prem"] += 1
            self._stats["total_rejected"] += 1
            return result

        self._stats["gate_passed"] += 1
        return _PASS

    def get_stats(self) -> dict:
        return dict(self._stats)

    def is_active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # Individual gates (return None = passed, HardGateResult = rejected)
    # ------------------------------------------------------------------

    def _gate_sweep(self, ev) -> Optional[HardGateResult]:
        """Gate 1: Only SWEEP trade types pass."""
        trade_type = getattr(ev, "trade_type", "") or ""
        if trade_type != "SWEEP":
            return HardGateResult(
                verdict=GateVerdict.REJECTED,
                rejected_by="sweep_only",
                reason=f"trade_type={trade_type!r} is not SWEEP",
                is_non_sweep=(trade_type != "SWEEP"),
            )
        return None

    def _gate_aggression(self, ev) -> Optional[HardGateResult]:
        """
        Gate 2: Aggression check.
        Calls must fill AT_ASK or ABOVE_ASK.
        Puts must fill AT_BID or BELOW_BID.
        Synthetic quotes (bid=ask=0) bypass this gate — aggression unknown.
        """
        if getattr(ev, "is_synthetic_quote", False):
            return None  # cannot assert aggression without real quotes

        ctype    = getattr(ev, "contract_type", "") or ""
        ba_class = getattr(ev, "bid_ask_class",  "") or ""

        if ctype == "CALL" and ba_class not in _CALL_PASS_CLASSES:
            return HardGateResult(
                verdict=GateVerdict.REJECTED,
                rejected_by="aggression",
                reason=f"CALL fill at {ba_class!r} — not AT_ASK/ABOVE_ASK",
            )
        if ctype == "PUT" and ba_class not in _PUT_PASS_CLASSES:
            return HardGateResult(
                verdict=GateVerdict.REJECTED,
                rejected_by="aggression",
                reason=f"PUT fill at {ba_class!r} — not AT_BID/BELOW_BID",
            )
        return None

    def _gate_volume_oi(self, ev) -> Optional[HardGateResult]:
        """
        Gate 3: Size must exceed open interest (new-bet filter).
        Skip if OI is 0 (not yet enriched from registry) — benefit of doubt.
        """
        oi   = int(getattr(ev, "open_interest", 0) or 0)
        size = int(getattr(ev, "size",           0) or 0)

        if oi > 0 and size <= oi:
            return HardGateResult(
                verdict=GateVerdict.REJECTED,
                rejected_by="volume_oi",
                reason=f"size={size} <= open_interest={oi}",
            )
        return None

    def _gate_spread(self, ev) -> Optional[HardGateResult]:
        """
        Gate 4: Spread must be <= MAX_SPREAD_RATIO.
        (ask - bid) / mid > threshold -> illiquid contract, skip.
        Skip if bid=ask=0 (synthetic quote).
        """
        bid = float(getattr(ev, "bid", 0) or 0)
        ask = float(getattr(ev, "ask", 0) or 0)

        if bid == 0 and ask == 0:
            return None  # synthetic quote — cannot compute spread

        if ask <= bid:
            return None  # inverted/crossed market — skip spread gate

        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None

        spread_ratio = (ask - bid) / mid
        if spread_ratio > self._max_spread_ratio:
            return HardGateResult(
                verdict=GateVerdict.REJECTED,
                rejected_by="spread",
                reason=f"spread_ratio={spread_ratio:.3f} > {self._max_spread_ratio}",
            )
        return None

    def _gate_premium(self, ev) -> Optional[HardGateResult]:
        """Gate 5: Individual trade premium must meet minimum threshold."""
        premium = float(getattr(ev, "premium", 0) or 0)
        if premium < self._min_premium:
            return HardGateResult(
                verdict=GateVerdict.REJECTED,
                rejected_by="min_premium",
                reason=f"premium=${premium:,.0f} < min=${self._min_premium:,.0f}",
            )
        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
apex_gate = SignalGate()
