"""
Tests for signals/signal_gate.py — Apex Phase 1 hard rejection gates.

Each gate is tested in isolation (all other fields set to pass)
and in combination to verify fail-fast ordering.
"""
import pytest
from types import SimpleNamespace
from signals import signal_gate
from signals.signal_gate import check, GateVerdict, reset_stats, stats


def _make_event(**overrides):
    """Returns a minimal OptionsFlowEvent-like object that passes all gates."""
    defaults = dict(
        trade_type       = "SWEEP",
        bid              = 1.00,
        ask              = 1.10,        # spread = 10/1.05 ≈ 9.5% < 15%
        fill_price       = 1.05,
        size             = 100,
        premium          = 10_500.0,    # > $5K minimum
        open_interest    = 50,
        daily_volume     = 200,         # > OI
        contract_type    = "CALL",
        bid_ask_class    = "AT_ASK",    # aggressive call
        is_synthetic_quote = False,
        conviction_score = 0.8,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def reset():
    reset_stats()
    # Reset env-driven config to defaults
    signal_gate.MAX_SPREAD_PCT      = 0.15
    signal_gate.MIN_TRADE_PREMIUM   = 5_000.0
    signal_gate.AGGRESSION_HARD_REJECT = False
    yield


# ---------------------------------------------------------------------------
# Gate 1 — Sweep only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ttype", ["BLOCK", "SINGLE", "SPLIT", "", "block"])
def test_sweep_gate_rejects_non_sweeps(ttype):
    ev = _make_event(trade_type=ttype)
    result = check(ev)
    assert result.verdict == GateVerdict.HARD_REJECT
    assert result.failed_gate == "sweep_only"


def test_sweep_gate_passes_sweep():
    ev = _make_event(trade_type="SWEEP")
    result = check(ev)
    assert result.verdict != GateVerdict.HARD_REJECT or result.failed_gate != "sweep_only"


# ---------------------------------------------------------------------------
# Gate 2 — Spread
# ---------------------------------------------------------------------------

def test_spread_gate_rejects_wide_spread():
    # (2.00 - 1.00) / 1.50 = 66.7% > 15%
    ev = _make_event(bid=1.00, ask=2.00, is_synthetic_quote=False)
    result = check(ev)
    assert result.hard_rejected
    assert result.failed_gate == "spread"


def test_spread_gate_passes_tight_spread():
    # (1.10 - 1.00) / 1.05 ≈ 9.5% < 15%
    ev = _make_event(bid=1.00, ask=1.10)
    result = check(ev)
    assert result.failed_gate != "spread"


def test_spread_gate_skips_synthetic_quotes():
    ev = _make_event(bid=0.0, ask=3.0, is_synthetic_quote=True)
    result = check(ev)
    assert result.failed_gate != "spread"


# ---------------------------------------------------------------------------
# Gate 3 — Min premium
# ---------------------------------------------------------------------------

def test_min_premium_rejects_below_threshold():
    ev = _make_event(premium=4_999.0)
    result = check(ev)
    assert result.hard_rejected
    assert result.failed_gate == "min_premium"


def test_min_premium_passes_at_threshold():
    ev = _make_event(premium=5_000.0)
    result = check(ev)
    assert result.failed_gate != "min_premium"


def test_min_premium_configurable(monkeypatch):
    monkeypatch.setattr(signal_gate, "MIN_TRADE_PREMIUM", 10_000.0)
    ev = _make_event(premium=7_500.0)
    result = check(ev)
    assert result.failed_gate == "min_premium"


# ---------------------------------------------------------------------------
# Gate 4 — Volume > OI
# ---------------------------------------------------------------------------

def test_vol_oi_rejects_when_volume_le_oi():
    ev = _make_event(open_interest=500, daily_volume=500)
    result = check(ev)
    assert result.hard_rejected
    assert result.failed_gate == "volume_oi"


def test_vol_oi_passes_when_volume_gt_oi():
    ev = _make_event(open_interest=500, daily_volume=501)
    result = check(ev)
    assert result.failed_gate != "volume_oi"


def test_vol_oi_skips_when_oi_zero():
    ev = _make_event(open_interest=0, daily_volume=0)
    result = check(ev)
    assert result.failed_gate != "volume_oi"


# ---------------------------------------------------------------------------
# Gate 5 — Aggression (soft reject by default)
# ---------------------------------------------------------------------------

def test_aggression_soft_rejects_call_at_mid():
    ev = _make_event(contract_type="CALL", bid_ask_class="MID")
    result = check(ev)
    assert result.soft_rejected
    assert result.failed_gate == "aggression"
    assert result.score_penalty == 0.25


def test_aggression_soft_rejects_put_at_mid():
    ev = _make_event(contract_type="PUT", bid_ask_class="MID")
    result = check(ev)
    assert result.soft_rejected
    assert result.failed_gate == "aggression"


def test_aggression_passes_call_at_ask():
    ev = _make_event(contract_type="CALL", bid_ask_class="AT_ASK")
    result = check(ev)
    assert result.verdict == GateVerdict.PASS


def test_aggression_passes_put_at_bid():
    ev = _make_event(contract_type="PUT", bid_ask_class="AT_BID")
    result = check(ev)
    assert result.verdict == GateVerdict.PASS


def test_aggression_becomes_hard_reject_when_flag_set(monkeypatch):
    monkeypatch.setattr(signal_gate, "AGGRESSION_HARD_REJECT", True)
    ev = _make_event(contract_type="CALL", bid_ask_class="MID")
    result = check(ev)
    assert result.hard_rejected
    assert result.failed_gate == "aggression"


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------

def test_stats_track_correctly():
    check(_make_event(trade_type="BLOCK"))   # hard reject gate 1
    check(_make_event(bid=1.0, ask=3.0))    # hard reject gate 2
    check(_make_event())                     # pass
    s = stats()
    assert s["gate_total_seen"] == 3
    assert s["gate_hard_rejected"] == 2
    assert s["gate_passed"] == 1


# ---------------------------------------------------------------------------
# Fail-fast ordering — gate 1 must fire before gate 3 etc.
# ---------------------------------------------------------------------------

def test_fail_fast_sweep_gate_fires_before_premium_gate():
    ev = _make_event(trade_type="BLOCK", premium=0.0)
    result = check(ev)
    assert result.failed_gate == "sweep_only"  # sweep gate fires first
