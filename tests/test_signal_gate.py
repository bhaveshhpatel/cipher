"""Tests for signals/signal_gate.py — Apex L1 signal gate.

Coverage target: 100% line + branch.

Scenarios covered (mapped to QA path coverage spec):
    QA-06  spread gate rejection
    QA-07  T1 SWEEP below premium floor
    QA-08  T2/T3 SINGLE below premium floor
    QA-10  T1 SWEEP passes gate (sweep bypass context in accumulator)
    Additional branches:
        - ask == 0  (zero-ask guard — spread check skipped)
        - T1 BLOCK / SPLIT / SINGLE pass
        - T2/T3 SWEEP / BLOCK / SPLIT pass
        - unknown trade_type sentinel rejection
        - unknown tier falls back to tier-3 floors
        - SELL PUT premium passes the gate (direction agnostic at L1)
"""

import sys
import os
import types
import pytest

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from repo root without installing the package
# ---------------------------------------------------------------------------
_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

from signals.signal_gate import GateVerdict, passes_signal_gate  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal OptionsFlowEvent stand-in
# ---------------------------------------------------------------------------

class _Event:
    """Minimal stand-in for OptionsFlowEvent — only the fields L1 reads."""

    def __init__(
        self,
        ask: float,
        bid: float,
        premium: float,
        trade_type: str,
    ) -> None:
        self.ask = ask
        self.bid = bid
        self.premium = premium
        self.trade_type = trade_type


# Helper so tests stay concise
def _ev(
    ask: float = 1.00,
    bid: float = 0.50,
    premium: float = 100_000,
    trade_type: str = "SWEEP",
) -> _Event:
    return _Event(ask=ask, bid=bid, premium=premium, trade_type=trade_type)


# ===========================================================================
# Spread gate tests  (QA-06)
# ===========================================================================

class TestSpreadGate:
    """Spread > 50% of ask → reject, regardless of tier and premium."""

    def test_spread_exactly_at_50pct_passes(self):
        # spread == 50% exactly is NOT > 50%, so it must pass the spread check.
        # Premium is large enough to also pass the premium gate.
        ev = _ev(ask=2.00, bid=1.00, premium=1_000_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is True
        assert verdict.reason == "passed"

    def test_spread_just_above_50pct_rejected(self):
        # bid=0.99, ask=2.00 → spread_pct = 1.01/2.00 = 0.505 > 0.50
        ev = _ev(ask=2.00, bid=0.99, premium=1_000_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is False
        assert verdict.reason == "spread_too_wide"

    def test_zero_bid_wide_spread_rejected(self):
        # bid=0, ask=1.0 → spread_pct = 1.0 > 0.50
        ev = _ev(ask=1.00, bid=0.00, premium=1_000_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is False
        assert verdict.reason == "spread_too_wide"

    def test_zero_ask_skips_spread_check(self):
        # ask == 0 → spread branch is skipped; only premium gate evaluated.
        # Premium is above T1 SWEEP floor (50K), so should pass.
        ev = _ev(ask=0.00, bid=0.00, premium=100_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is True
        assert verdict.reason == "passed"

    def test_spread_rejected_on_tier2_as_well(self):
        ev = _ev(ask=4.00, bid=0.00, premium=500_000, trade_type="BLOCK")
        verdict = passes_signal_gate(ev, tier=2)
        assert verdict.passed is False
        assert verdict.reason == "spread_too_wide"


# ===========================================================================
# Premium floor tests — T1  (QA-07)
# ===========================================================================

class TestT1PremiumFloors:
    """T1 floor table: SWEEP>=50K, BLOCK>=100K, SPLIT>=150K, SINGLE>=250K."""

    def test_t1_sweep_below_floor_rejected(self):
        ev = _ev(ask=1.00, bid=0.50, premium=49_999, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is False
        assert verdict.reason == "premium_below_floor"

    def test_t1_sweep_at_floor_passes(self):
        ev = _ev(ask=1.00, bid=0.50, premium=50_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is True

    def test_t1_block_below_floor_rejected(self):
        ev = _ev(premium=99_999, trade_type="BLOCK")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is False

    def test_t1_block_at_floor_passes(self):
        ev = _ev(premium=100_000, trade_type="BLOCK")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is True

    def test_t1_split_below_floor_rejected(self):
        ev = _ev(premium=149_999, trade_type="SPLIT")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is False

    def test_t1_split_at_floor_passes(self):
        ev = _ev(premium=150_000, trade_type="SPLIT")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is True

    def test_t1_single_below_floor_rejected(self):
        ev = _ev(premium=249_999, trade_type="SINGLE")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is False

    def test_t1_single_at_floor_passes(self):
        ev = _ev(premium=250_000, trade_type="SINGLE")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is True


# ===========================================================================
# Premium floor tests — T2/T3  (QA-08)
# ===========================================================================

class TestT2T3PremiumFloors:
    """T2/T3 share floor table: SWEEP>=25K, BLOCK>=50K, SPLIT>=100K, SINGLE>=150K."""

    def test_t2_sweep_below_floor_rejected(self):
        ev = _ev(premium=24_999, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=2)
        assert verdict.passed is False
        assert verdict.reason == "premium_below_floor"

    def test_t2_sweep_at_floor_passes(self):
        ev = _ev(premium=25_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=2)
        assert verdict.passed is True

    def test_t3_single_below_floor_rejected(self):
        """QA-08: T3 SINGLE below $150K floor."""
        ev = _ev(premium=149_999, trade_type="SINGLE")
        verdict = passes_signal_gate(ev, tier=3)
        assert verdict.passed is False
        assert verdict.reason == "premium_below_floor"

    def test_t3_single_at_floor_passes(self):
        ev = _ev(premium=150_000, trade_type="SINGLE")
        verdict = passes_signal_gate(ev, tier=3)
        assert verdict.passed is True

    def test_t2_t3_floors_are_identical(self):
        """T2 and T3 must enforce the same floor table."""
        for trade_type, floor in [("SWEEP", 25_000), ("BLOCK", 50_000),
                                   ("SPLIT", 100_000), ("SINGLE", 150_000)]:
            ev_pass_t2 = _ev(premium=floor, trade_type=trade_type)
            ev_pass_t3 = _ev(premium=floor, trade_type=trade_type)
            assert passes_signal_gate(ev_pass_t2, tier=2).passed is True, (
                f"T2 {trade_type} at exact floor should pass"
            )
            assert passes_signal_gate(ev_pass_t3, tier=3).passed is True, (
                f"T3 {trade_type} at exact floor should pass"
            )


# ===========================================================================
# Unknown trade type
# ===========================================================================

class TestUnknownTradeType:
    """An unrecognised trade_type falls back to the sentinel floor and is always rejected."""

    def test_unknown_trade_type_always_rejected(self):
        ev = _ev(premium=999_999_998, trade_type="ICEBERG")  # below sentinel by 1
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is False
        assert verdict.reason == "premium_below_floor"


# ===========================================================================
# Unknown tier falls back to tier-3 floors
# ===========================================================================

class TestUnknownTier:
    """An unrecognised tier defaults to the tier-3 floor table."""

    def test_unknown_tier_uses_tier3_sweep_floor(self):
        # 25_000 exactly clears the T3 SWEEP floor.
        ev = _ev(premium=25_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=99)  # unmapped tier
        assert verdict.passed is True

    def test_unknown_tier_below_tier3_floor_rejected(self):
        ev = _ev(premium=24_999, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=99)
        assert verdict.passed is False
        assert verdict.reason == "premium_below_floor"


# ===========================================================================
# Direction-agnostic gate — SELL PUT passes if premium and spread are sound
# ===========================================================================

class TestDirectionAgnostic:
    """L1 is direction-agnostic; high-quality SELL PUT flow must pass."""

    def test_sell_put_passes_gate(self):
        """High-quality SELL PUT (bullish) must not be blocked at L1."""
        # Simulate AT_BID PUT SWEEP, T1, 600K premium, tight spread.
        ev = _ev(ask=5.00, bid=4.80, premium=600_000, trade_type="SWEEP")
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.passed is True
        assert verdict.reason == "passed"


# ===========================================================================
# GateVerdict NamedTuple contract
# ===========================================================================

class TestGateVerdictContract:
    """GateVerdict is a NamedTuple; callers must be able to unpack and index it."""

    def test_verdict_is_named_tuple(self):
        v = GateVerdict(passed=True, reason="passed")
        passed, reason = v
        assert passed is True
        assert reason == "passed"

    def test_verdict_field_access(self):
        v = GateVerdict(passed=False, reason="spread_too_wide")
        assert v.passed is False
        assert v.reason == "spread_too_wide"
