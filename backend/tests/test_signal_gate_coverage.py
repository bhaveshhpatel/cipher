"""
test_signal_gate_coverage.py

Covers signals/signal_gate.py (was at 0%).

Tests:
  - passes_signal_gate returns GateVerdict(True, 'passed') for a clean event
  - spread gate fires when spread_pct > 0.50
  - spread gate skipped when ask == 0
  - premium floor fires when premium < floor
  - unknown tier clamped to tier-3 floors
  - unknown trade_type uses sentinel -> fails
  - GateVerdict is a NamedTuple with .passed and .reason
"""
from unittest.mock import SimpleNamespace
from signals.signal_gate import passes_signal_gate, GateVerdict, _PREMIUM_FLOORS


def _ev(ask=1.00, bid=0.80, premium=60_000, trade_type="SWEEP"):
    return SimpleNamespace(ask=ask, bid=bid, premium=premium, trade_type=trade_type)


def test_gate_passes_clean_t1_sweep():
    verdict = passes_signal_gate(_ev(ask=1.00, bid=0.90, premium=60_000, trade_type="SWEEP"), tier=1)
    assert verdict.passed is True
    assert verdict.reason == "passed"


def test_gate_verdict_is_named_tuple():
    v = passes_signal_gate(_ev(), tier=1)
    assert isinstance(v, GateVerdict)
    assert hasattr(v, "passed")
    assert hasattr(v, "reason")


def test_spread_gate_fires():
    # spread_pct = (2.00 - 0.50) / 2.00 = 0.75 > 0.50
    verdict = passes_signal_gate(_ev(ask=2.00, bid=0.50, premium=999_999), tier=1)
    assert verdict.passed is False
    assert verdict.reason == "spread_too_wide"


def test_spread_gate_skipped_when_ask_zero():
    # ask == 0 -> spread gate skipped entirely; premium gate decides
    verdict = passes_signal_gate(_ev(ask=0, bid=0, premium=60_000, trade_type="SWEEP"), tier=1)
    assert verdict.passed is True


def test_premium_floor_fails_t1_block():
    # T1 BLOCK floor = 100_000; premium=50_000 < floor
    verdict = passes_signal_gate(_ev(ask=1.00, bid=0.90, premium=50_000, trade_type="BLOCK"), tier=1)
    assert verdict.passed is False
    assert verdict.reason == "premium_below_floor"


def test_premium_floor_passes_t1_block():
    verdict = passes_signal_gate(_ev(ask=1.00, bid=0.90, premium=100_001, trade_type="BLOCK"), tier=1)
    assert verdict.passed is True


def test_t2_lower_floor():
    # T2 SWEEP floor = 25_000
    verdict = passes_signal_gate(_ev(ask=1.00, bid=0.90, premium=26_000, trade_type="SWEEP"), tier=2)
    assert verdict.passed is True


def test_t3_same_as_t2():
    assert _PREMIUM_FLOORS[2] == _PREMIUM_FLOORS[3]
    verdict = passes_signal_gate(_ev(ask=1.00, bid=0.90, premium=26_000, trade_type="SWEEP"), tier=3)
    assert verdict.passed is True


def test_unknown_tier_clamped_to_t3():
    # tier=99 unknown -> clamped to tier-3 floors (most permissive)
    verdict = passes_signal_gate(_ev(ask=1.00, bid=0.90, premium=26_000, trade_type="SWEEP"), tier=99)
    assert verdict.passed is True


def test_unknown_trade_type_uses_sentinel_fails():
    # unknown trade_type -> _UNKNOWN_FLOOR_SENTINEL = 999_999_999; any realistic premium fails
    verdict = passes_signal_gate(_ev(ask=1.00, bid=0.90, premium=1_000_000, trade_type="EXOTIC"), tier=1)
    assert verdict.passed is False
    assert verdict.reason == "premium_below_floor"


def test_all_t1_trade_types_have_floors():
    for tt, floor in _PREMIUM_FLOORS[1].items():
        v_fail = passes_signal_gate(_ev(ask=1.0, bid=0.95, premium=floor - 1, trade_type=tt), tier=1)
        assert v_fail.passed is False, f"{tt} should fail below floor"
        v_pass = passes_signal_gate(_ev(ask=1.0, bid=0.95, premium=floor, trade_type=tt), tier=1)
        assert v_pass.passed is True, f"{tt} should pass at floor"
