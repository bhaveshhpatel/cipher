"""
tests/test_signal_gate.py

Full coverage for signals/signal_gate.py including:
  - All 5 gate hard-reject paths
  - Proportional aggression penalty math
  - Flat fallback when price data absent
  - Penalty cap at MAX_AGGRESSION_PENALTY
  - Hard-reject mode via runtime toggle
  - Runtime override helpers (set / reset)
  - stats() and reset_stats()
  - Pass-through (all gates clear)
"""
import pytest
from unittest.mock import MagicMock
from signals import signal_gate
from signals.signal_gate import (
    GateVerdict,
    GateResult,
    check,
    stats,
    reset_stats,
    get_aggression_hard_reject,
    set_aggression_hard_reject,
    reset_aggression_override,
    _compute_aggression_penalty,
    FLAT_AGGRESSION_PENALTY,
    MAX_AGGRESSION_PENALTY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ev(**kwargs):
    """Build a minimal mock OptionsFlowEvent."""
    ev = MagicMock()
    # sensible defaults that clear all gates
    ev.trade_type       = kwargs.get("trade_type",       "SWEEP")
    ev.bid              = kwargs.get("bid",              1.00)
    ev.ask              = kwargs.get("ask",              1.10)
    ev.fill_price       = kwargs.get("fill_price",       1.10)  # at ask
    ev.is_synthetic_quote = kwargs.get("is_synthetic_quote", False)
    ev.premium          = kwargs.get("premium",          10_000)
    ev.open_interest    = kwargs.get("open_interest",    0)     # 0 = skip vol/OI check
    ev.daily_volume     = kwargs.get("daily_volume",     None)
    ev.size             = kwargs.get("size",             0)
    ev.bid_ask_class    = kwargs.get("bid_ask_class",    "AT_ASK")
    ev.contract_type    = kwargs.get("contract_type",   "CALL")
    return ev


@pytest.fixture(autouse=True)
def _clean():
    reset_stats()
    reset_aggression_override()
    yield
    reset_stats()
    reset_aggression_override()


# ---------------------------------------------------------------------------
# Gate 1 — sweep only
# ---------------------------------------------------------------------------

def test_gate1_rejects_non_sweep():
    ev = _ev(trade_type="SINGLE")
    r  = check(ev)
    assert r.hard_rejected
    assert r.failed_gate == "sweep_only"
    assert stats()["gate_rejected_sweep_only"] == 1


def test_gate1_passes_sweep():
    ev = _ev(trade_type="SWEEP")
    r  = check(ev)
    assert r.passed


def test_gate1_case_insensitive():
    ev = _ev(trade_type="sweep")
    r  = check(ev)
    assert r.passed


# ---------------------------------------------------------------------------
# Gate 2 — spread
# ---------------------------------------------------------------------------

def test_gate2_rejects_wide_spread():
    ev = _ev(bid=1.00, ask=2.00)   # spread = 66.7%
    r  = check(ev)
    assert r.hard_rejected
    assert r.failed_gate == "spread"


def test_gate2_passes_tight_spread():
    ev = _ev(bid=1.00, ask=1.10)
    r  = check(ev)
    assert r.passed


def test_gate2_skips_synthetic_quote():
    ev = _ev(bid=1.00, ask=2.00, is_synthetic_quote=True)
    r  = check(ev)
    assert r.passed


def test_gate2_skips_zero_bid():
    ev = _ev(bid=0, ask=1.10)
    r  = check(ev)
    assert r.passed


def test_gate2_skips_zero_ask():
    ev = _ev(bid=1.00, ask=0)
    r  = check(ev)
    assert r.passed


# ---------------------------------------------------------------------------
# Gate 3 — min premium
# ---------------------------------------------------------------------------

def test_gate3_rejects_low_premium():
    ev = _ev(premium=100)
    r  = check(ev)
    assert r.hard_rejected
    assert r.failed_gate == "min_premium"


def test_gate3_passes_sufficient_premium():
    ev = _ev(premium=5_000)
    r  = check(ev)
    assert r.passed


# ---------------------------------------------------------------------------
# Gate 4 — volume > OI
# ---------------------------------------------------------------------------

def test_gate4_rejects_when_vol_lte_oi():
    ev = _ev(open_interest=1000, daily_volume=500)
    r  = check(ev)
    assert r.hard_rejected
    assert r.failed_gate == "volume_oi"


def test_gate4_rejects_vol_equals_oi():
    ev = _ev(open_interest=1000, daily_volume=1000)
    r  = check(ev)
    assert r.hard_rejected


def test_gate4_passes_when_vol_gt_oi():
    ev = _ev(open_interest=500, daily_volume=1000)
    r  = check(ev)
    assert r.passed


def test_gate4_skips_zero_oi():
    ev = _ev(open_interest=0, daily_volume=0)
    r  = check(ev)
    assert r.passed


def test_gate4_uses_size_fallback():
    ev = _ev(open_interest=500, daily_volume=None, size=1000)
    r  = check(ev)
    assert r.passed


# ---------------------------------------------------------------------------
# Gate 5 — aggression (soft, proportional penalty)
# ---------------------------------------------------------------------------

def test_gate5_call_at_ask_passes():
    ev = _ev(bid_ask_class="AT_ASK", contract_type="CALL")
    r  = check(ev)
    assert r.passed


def test_gate5_call_above_ask_passes():
    ev = _ev(bid_ask_class="ABOVE_ASK", contract_type="CALL")
    r  = check(ev)
    assert r.passed


def test_gate5_put_at_bid_passes():
    ev = _ev(bid_ask_class="AT_BID", contract_type="PUT")
    r  = check(ev)
    assert r.passed


def test_gate5_put_below_bid_passes():
    ev = _ev(bid_ask_class="BELOW_BID", contract_type="PUT")
    r  = check(ev)
    assert r.passed


def test_gate5_call_mid_soft_rejects():
    ev = _ev(bid_ask_class="MID", contract_type="CALL")
    r  = check(ev)
    assert r.soft_rejected
    assert r.failed_gate == "aggression"
    assert 0 < r.score_penalty <= MAX_AGGRESSION_PENALTY


def test_gate5_put_above_ask_soft_rejects():
    ev = _ev(bid_ask_class="ABOVE_ASK", contract_type="PUT")
    r  = check(ev)
    assert r.soft_rejected


def test_gate5_empty_ba_class_passes():
    ev = _ev(bid_ask_class="", contract_type="CALL")
    r  = check(ev)
    assert r.passed


def test_gate5_empty_contract_type_passes():
    ev = _ev(bid_ask_class="MID", contract_type="")
    r  = check(ev)
    assert r.passed


def test_gate5_hard_reject_mode_via_override():
    set_aggression_hard_reject(True)
    ev = _ev(bid_ask_class="MID", contract_type="CALL")
    r  = check(ev)
    assert r.hard_rejected
    assert r.failed_gate == "aggression"


def test_gate5_soft_mode_after_reset():
    set_aggression_hard_reject(True)
    reset_aggression_override()
    ev = _ev(bid_ask_class="MID", contract_type="CALL")
    r  = check(ev)
    assert r.soft_rejected


# ---------------------------------------------------------------------------
# Proportional penalty math
# ---------------------------------------------------------------------------

def test_penalty_call_filled_below_ask():
    """
    ask=2.00, fill=1.80 → distance=(2.00-1.80)/2.00 = 0.10
    Should be between FLAT_PENALTY and MAX_PENALTY.
    """
    ev = _ev(bid=1.70, ask=2.00, fill_price=1.80, contract_type="CALL")
    penalty = _compute_aggression_penalty(ev, "CALL")
    assert abs(penalty - 0.10) < 1e-9


def test_penalty_put_filled_above_bid():
    """
    bid=1.00, fill=1.20 → distance=(1.20-1.00)/1.00 = 0.20
    """
    ev = _ev(bid=1.00, ask=1.50, fill_price=1.20, contract_type="PUT")
    penalty = _compute_aggression_penalty(ev, "PUT")
    assert abs(penalty - 0.20) < 1e-9


def test_penalty_capped_at_max():
    """
    ask=2.00, fill=0.50 → distance=(2.00-0.50)/2.00 = 0.75 → capped at MAX (0.40)
    """
    ev = _ev(bid=0.40, ask=2.00, fill_price=0.50, contract_type="CALL")
    penalty = _compute_aggression_penalty(ev, "CALL")
    assert penalty == MAX_AGGRESSION_PENALTY


def test_penalty_floor_at_flat_when_small_distance():
    """
    ask=2.00, fill=1.99 → distance=0.005 → below FLAT_PENALTY floor → returns FLAT
    """
    ev = _ev(bid=1.90, ask=2.00, fill_price=1.99, contract_type="CALL")
    penalty = _compute_aggression_penalty(ev, "CALL")
    assert penalty == FLAT_AGGRESSION_PENALTY


def test_penalty_fallback_no_fill_price():
    ev = _ev(fill_price=0, contract_type="CALL")
    penalty = _compute_aggression_penalty(ev, "CALL")
    assert penalty == FLAT_AGGRESSION_PENALTY


def test_penalty_fallback_no_ask_for_call():
    ev = _ev(ask=0, fill_price=1.10, contract_type="CALL")
    penalty = _compute_aggression_penalty(ev, "CALL")
    assert penalty == FLAT_AGGRESSION_PENALTY


def test_penalty_fallback_no_bid_for_put():
    ev = _ev(bid=0, fill_price=1.10, contract_type="PUT")
    penalty = _compute_aggression_penalty(ev, "PUT")
    assert penalty == FLAT_AGGRESSION_PENALTY


def test_penalty_zero_distance_returns_flat():
    """fill exactly at ask — distance=0, shouldn't reach here but guard tested."""
    ev = _ev(bid=1.00, ask=2.00, fill_price=2.00, contract_type="CALL")
    penalty = _compute_aggression_penalty(ev, "CALL")
    assert penalty == FLAT_AGGRESSION_PENALTY


# ---------------------------------------------------------------------------
# Proportional penalty applied in full check() path
# ---------------------------------------------------------------------------

def test_full_check_penalty_proportional():
    """
    ask=2.00, fill=1.80 → penalty should be 0.10 on the SOFT_REJECT result.
    """
    ev = _ev(
        bid_ask_class="MID",
        contract_type="CALL",
        bid=1.70,
        ask=2.00,
        fill_price=1.80,
    )
    r = check(ev)
    assert r.soft_rejected
    assert abs(r.score_penalty - 0.10) < 1e-9


# ---------------------------------------------------------------------------
# Runtime override helpers
# ---------------------------------------------------------------------------

def test_get_aggression_hard_reject_default():
    assert get_aggression_hard_reject() is False


def test_set_and_get_override_true():
    set_aggression_hard_reject(True)
    assert get_aggression_hard_reject() is True


def test_set_and_get_override_false():
    set_aggression_hard_reject(False)
    assert get_aggression_hard_reject() is False


def test_reset_override_restores_env_default():
    set_aggression_hard_reject(True)
    reset_aggression_override()
    # env default is False (not set in test env)
    assert get_aggression_hard_reject() is False
    assert signal_gate._aggression_hard_reject_override is None


# ---------------------------------------------------------------------------
# stats() and reset_stats()
# ---------------------------------------------------------------------------

def test_stats_includes_hard_reject_flag():
    s = stats()
    assert "aggression_hard_reject" in s
    assert s["aggression_hard_reject"] is False


def test_stats_reflect_override():
    set_aggression_hard_reject(True)
    assert stats()["aggression_hard_reject"] is True


def test_reset_stats_zeroes_counters():
    check(_ev(trade_type="SINGLE"))
    reset_stats()
    s = stats()
    assert s["gate_total_seen"] == 0
    assert s["gate_hard_rejected"] == 0


def test_stats_passed_counter():
    check(_ev())
    assert stats()["gate_passed"] == 1


def test_stats_soft_rejected_counter():
    check(_ev(bid_ask_class="MID", contract_type="CALL"))
    assert stats()["gate_soft_rejected"] == 1
    assert stats()["gate_flagged_aggression"] == 1


# ---------------------------------------------------------------------------
# Full pass-through
# ---------------------------------------------------------------------------

def test_full_pass():
    ev = _ev(
        trade_type="SWEEP",
        bid=1.00,
        ask=1.10,
        fill_price=1.10,
        premium=10_000,
        open_interest=0,
        bid_ask_class="AT_ASK",
        contract_type="CALL",
    )
    r = check(ev)
    assert r.passed
    assert r.score_penalty == 0.0
    assert stats()["gate_passed"] == 1
    assert stats()["gate_total_seen"] == 1
