"""
Tests for signals/signal_gate.py — Apex Phase 1 hard rejection gates.

Covers:
  - All 5 gates individually (happy path + reject path + edge cases)
  - GateResult property API (.passed, .hard_rejected, .soft_rejected)
  - Stats tracking per gate + reset_stats()
  - Fail-fast ordering
  - AGGRESSION_HARD_REJECT env-flag flip
  - Configurable thresholds via monkeypatch
"""
import pytest
from types import SimpleNamespace
from signals import signal_gate
from signals.signal_gate import check, GateVerdict, reset_stats, stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(**overrides):
    """Returns a minimal OptionsFlowEvent-like object that passes all gates."""
    defaults = dict(
        trade_type         = "SWEEP",
        bid                = 1.00,
        ask                = 1.10,        # spread ~ 9.5% < 15%
        fill_price         = 1.05,
        size               = 100,
        premium            = 10_500.0,    # > $5K minimum
        open_interest      = 50,
        daily_volume       = 200,         # > OI
        contract_type      = "CALL",
        bid_ask_class      = "AT_ASK",    # aggressive call
        is_synthetic_quote = False,
        conviction_score   = 0.8,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def reset():
    reset_stats()
    signal_gate.MAX_SPREAD_PCT         = 0.15
    signal_gate.MIN_TRADE_PREMIUM      = 5_000.0
    signal_gate.AGGRESSION_HARD_REJECT = False
    yield


# ---------------------------------------------------------------------------
# GateResult property API
# ---------------------------------------------------------------------------

def test_gate_result_passed_property():
    ev = _make_event()
    result = check(ev)
    assert result.passed is True
    assert result.hard_rejected is False
    assert result.soft_rejected is False


def test_gate_result_hard_rejected_property():
    ev = _make_event(trade_type="BLOCK")
    result = check(ev)
    assert result.hard_rejected is True
    assert result.passed is False
    assert result.soft_rejected is False


def test_gate_result_soft_rejected_property():
    ev = _make_event(contract_type="CALL", bid_ask_class="MID")
    result = check(ev)
    assert result.soft_rejected is True
    assert result.passed is False
    assert result.hard_rejected is False


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
    assert result.failed_gate != "sweep_only"


def test_sweep_gate_case_insensitive():
    """Lowercase 'sweep' should also pass gate 1."""
    ev = _make_event(trade_type="sweep")
    result = check(ev)
    assert result.failed_gate != "sweep_only"


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
    ev = _make_event(bid=1.00, ask=1.10)
    result = check(ev)
    assert result.failed_gate != "spread"


def test_spread_gate_skips_synthetic_quotes():
    """Synthetic quotes bypass spread check even with absurd spread."""
    ev = _make_event(bid=0.0, ask=3.0, is_synthetic_quote=True)
    result = check(ev)
    assert result.failed_gate != "spread"


def test_spread_gate_skips_when_bid_zero():
    """bid=0 → can't compute spread → pass through."""
    ev = _make_event(bid=0.0, ask=1.10, is_synthetic_quote=False)
    result = check(ev)
    assert result.failed_gate != "spread"


def test_spread_gate_skips_when_ask_zero():
    """ask=0 → can't compute spread → pass through."""
    ev = _make_event(bid=1.00, ask=0.0, is_synthetic_quote=False)
    result = check(ev)
    assert result.failed_gate != "spread"


def test_spread_gate_respects_custom_threshold(monkeypatch):
    monkeypatch.setattr(signal_gate, "MAX_SPREAD_PCT", 0.05)
    # spread ~ 9.5% now exceeds 5% threshold
    ev = _make_event(bid=1.00, ask=1.10, is_synthetic_quote=False)
    result = check(ev)
    assert result.failed_gate == "spread"


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


def test_min_premium_passes_above_threshold():
    ev = _make_event(premium=50_000.0)
    result = check(ev)
    assert result.failed_gate != "min_premium"


def test_min_premium_rejects_zero_premium():
    ev = _make_event(premium=0.0)
    result = check(ev)
    assert result.failed_gate == "min_premium"


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


def test_vol_oi_rejects_when_volume_lt_oi():
    ev = _make_event(open_interest=500, daily_volume=100)
    result = check(ev)
    assert result.hard_rejected
    assert result.failed_gate == "volume_oi"


def test_vol_oi_passes_when_volume_gt_oi():
    ev = _make_event(open_interest=500, daily_volume=501)
    result = check(ev)
    assert result.failed_gate != "volume_oi"


def test_vol_oi_skips_when_oi_zero():
    """OI=0 means data unavailable — pass through."""
    ev = _make_event(open_interest=0, daily_volume=0)
    result = check(ev)
    assert result.failed_gate != "volume_oi"


def test_vol_oi_falls_back_to_size_when_daily_volume_missing():
    """
    daily_volume absent → size used as proxy.
    size=100, OI=50 → volume > OI → should pass gate 4.
    """
    ev = _make_event(open_interest=50, size=100)
    del ev.daily_volume
    result = check(ev)
    assert result.failed_gate != "volume_oi"


def test_vol_oi_rejects_via_size_proxy():
    """
    daily_volume absent → size used as proxy.
    size=10, OI=100 → volume <= OI → hard reject.
    """
    ev = _make_event(open_interest=100, size=10)
    del ev.daily_volume
    result = check(ev)
    assert result.failed_gate == "volume_oi"


def test_vol_oi_skips_when_both_volume_and_size_zero():
    """If volume proxy is also 0, pass through (can't evaluate)."""
    ev = _make_event(open_interest=100, daily_volume=0, size=0)
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


def test_aggression_passes_call_above_ask():
    ev = _make_event(contract_type="CALL", bid_ask_class="ABOVE_ASK")
    result = check(ev)
    assert result.verdict == GateVerdict.PASS


def test_aggression_passes_put_at_bid():
    ev = _make_event(contract_type="PUT", bid_ask_class="AT_BID")
    result = check(ev)
    assert result.verdict == GateVerdict.PASS


def test_aggression_passes_put_below_bid():
    ev = _make_event(contract_type="PUT", bid_ask_class="BELOW_BID")
    result = check(ev)
    assert result.verdict == GateVerdict.PASS


def test_aggression_skips_when_ba_class_empty():
    """Unknown ba_class → gate cannot evaluate → pass through."""
    ev = _make_event(contract_type="CALL", bid_ask_class="")
    result = check(ev)
    assert result.failed_gate != "aggression"


def test_aggression_skips_when_contract_type_empty():
    """Unknown contract_type → gate cannot evaluate → pass through."""
    ev = _make_event(contract_type="", bid_ask_class="MID")
    result = check(ev)
    assert result.failed_gate != "aggression"


def test_aggression_becomes_hard_reject_when_flag_set(monkeypatch):
    monkeypatch.setattr(signal_gate, "AGGRESSION_HARD_REJECT", True)
    ev = _make_event(contract_type="CALL", bid_ask_class="MID")
    result = check(ev)
    assert result.hard_rejected
    assert result.failed_gate == "aggression"


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------

def test_stats_track_hard_rejects_and_passes():
    check(_make_event(trade_type="BLOCK"))   # hard reject gate 1
    check(_make_event(bid=1.0, ask=3.0))    # hard reject gate 2
    check(_make_event())                     # pass
    s = stats()
    assert s["gate_total_seen"] == 3
    assert s["gate_hard_rejected"] == 2
    assert s["gate_passed"] == 1
    assert s["gate_soft_rejected"] == 0


def test_stats_track_soft_rejects_separately():
    check(_make_event(contract_type="CALL", bid_ask_class="MID"))  # soft reject
    s = stats()
    assert s["gate_soft_rejected"] == 1
    assert s["gate_hard_rejected"] == 0
    assert s["gate_flagged_aggression"] == 1


def test_stats_per_gate_counters():
    check(_make_event(trade_type="BLOCK"))              # sweep counter
    check(_make_event(bid=1.0, ask=3.0))               # spread counter
    check(_make_event(premium=100.0))                  # min_premium counter
    check(_make_event(open_interest=500, daily_volume=100))  # vol_oi counter
    s = stats()
    assert s["gate_rejected_sweep_only"] == 1
    assert s["gate_rejected_spread"] == 1
    assert s["gate_rejected_min_premium"] == 1
    assert s["gate_rejected_vol_oi"] == 1


def test_reset_stats_clears_all_counters():
    check(_make_event(trade_type="BLOCK"))
    check(_make_event())
    reset_stats()
    s = stats()
    assert s["gate_total_seen"] == 0
    assert s["gate_hard_rejected"] == 0
    assert s["gate_passed"] == 0
    assert s["gate_soft_rejected"] == 0
    assert s["gate_rejected_sweep_only"] == 0
    assert s["gate_rejected_spread"] == 0
    assert s["gate_rejected_min_premium"] == 0
    assert s["gate_rejected_vol_oi"] == 0
    assert s["gate_flagged_aggression"] == 0


# ---------------------------------------------------------------------------
# Fail-fast ordering
# ---------------------------------------------------------------------------

def test_fail_fast_sweep_gate_fires_before_premium_gate():
    """Gate 1 must short-circuit before Gate 3."""
    ev = _make_event(trade_type="BLOCK", premium=0.0)
    result = check(ev)
    assert result.failed_gate == "sweep_only"


def test_fail_fast_sweep_gate_fires_before_spread_gate():
    """Gate 1 must short-circuit before Gate 2."""
    ev = _make_event(trade_type="BLOCK", bid=1.0, ask=3.0)
    result = check(ev)
    assert result.failed_gate == "sweep_only"


def test_fail_fast_spread_fires_before_premium():
    """Gate 2 must short-circuit before Gate 3."""
    ev = _make_event(bid=1.0, ask=3.0, premium=0.0)
    result = check(ev)
    assert result.failed_gate == "spread"


def test_fail_fast_premium_fires_before_vol_oi():
    """Gate 3 must short-circuit before Gate 4."""
    ev = _make_event(premium=100.0, open_interest=500, daily_volume=100)
    result = check(ev)
    assert result.failed_gate == "min_premium"


def test_all_gates_pass_gives_pass_verdict():
    """A fully clean event clears all 5 gates."""
    ev = _make_event()
    result = check(ev)
    assert result.verdict == GateVerdict.PASS
    assert result.failed_gate is None
    assert result.reason is None
    assert result.score_penalty == 0.0
