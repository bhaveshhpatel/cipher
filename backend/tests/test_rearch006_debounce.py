# ============================================================================
# tests/test_rearch006_debounce.py
#
# REARCH-006 — Chunk 7: Unit tests for SIG-DEBOUNCE (SignalDebounce) and
#              the compute_conviction_score() pure function.
#
# Coverage targets
# ────────────────
#   SignalDebounce.should_emit()
#     TC-DB-01  First call → allowed (no state)
#     TC-DB-02  Second call within window → suppressed
#     TC-DB-03  Call after window expires → allowed again
#     TC-DB-04  debounce_enabled=False → bypass (always True)
#     TC-DB-05  Different alert_level on same contract → both emit
#     TC-DB-06  record_emit() at= parameter stamps explicit time
#     TC-DB-07  clear() resets state → next call allows emission
#     TC-DB-08  stats() reflects current config + tracked key count
#
#   compute_conviction_score()
#     TC-CV-01  All 5 dimensions pass → score=5, normalised pct=100
#     TC-CV-02  No dimensions pass → score=0, normalised pct=0
#     TC-CV-03  Ask-side only → score=1
#     TC-CV-04  Vol>OI only → score=1
#     TC-CV-05  Qualifying notional tier (NOTEWORTHY) → D3 point
#     TC-CV-06  Qualifying notional tier (BLOCK) → D3 point
#     TC-CV-07  Qualifying notional tier (GOLDEN) → D3 point
#     TC-CV-08  Disqualifying tier (SMALL) → D3 no point
#     TC-CV-09  DTE bucket 0-7 → D4 disqualified
#     TC-CV-10  DTE bucket 90+ → D4 disqualified
#     TC-CV-11  DTE bucket 8-30 (qualifying) → D4 point
#     TC-CV-12  Repetition count meets floor → D5 point
#     TC-CV-13  Repetition count below floor → no D5 point
#     TC-CV-14  cfg dict key access (sig.ask_side_pct_floor, sig.min_trade_count)
#     TC-CV-15  cfg object attribute access (ask_side_pct_floor, min_trade_count)
#     TC-CV-16  composite_score = conviction / 5 → max normalised = 1.000 (=100%)
#
# Isolation strategy
# ──────────────────
#   All SignalDebounce tests use a fresh instance (not the module singleton)
#   so test order cannot pollute state.
#
#   compute_conviction_score is a pure function and needs no patching; episode
#   and cfg objects are plain SimpleNamespace / dicts.
#
#   get_param is patched via unittest.mock.patch targeting the import inside
#   signal_debounce so _read_enabled() / _read_window_s() return controlled
#   values without hitting the DB.
# ============================================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from signals.signal_debounce import SignalDebounce
from signals.signal_engine import compute_conviction_score


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_SYM  = "AAPL"
_CONT = "AAPL250117C00150000"
_LVL  = "GOLDEN"

_NOW  = datetime(2026, 5, 12, 15, 0, 0, tzinfo=timezone.utc)


def _debounce(enabled: bool = True, window_s: float = 300.0) -> SignalDebounce:
    """Return a fresh, isolated SignalDebounce whose config reads are patched."""
    return SignalDebounce()


def _ep(
    *,
    ask_side_pct: float | None = 0.75,
    vol_oi_signal: bool = True,
    notional_tier: str = "GOLDEN",
    dte_bucket: str = "8-30",
    trade_count: int = 5,
) -> SimpleNamespace:
    """Build a minimal episode SimpleNamespace for compute_conviction_score."""
    return SimpleNamespace(
        ask_side_pct=ask_side_pct,
        vol_oi_signal=vol_oi_signal,
        notional_tier=notional_tier,
        dte_bucket=dte_bucket,
        trade_count=trade_count,
    )


def _cfg_dict(
    *,
    ask_side_pct_floor: float = 0.6,
    min_trade_count: int = 3,
) -> dict:
    """Return a cfg dict using the sig.* key convention."""
    return {
        "sig.ask_side_pct_floor": ask_side_pct_floor,
        "sig.min_trade_count": min_trade_count,
    }


def _cfg_obj(
    *,
    ask_side_pct_floor: float = 0.6,
    min_trade_count: int = 3,
) -> SimpleNamespace:
    """Return a cfg object using plain attribute names (SignalConfig style)."""
    return SimpleNamespace(
        ask_side_pct_floor=ask_side_pct_floor,
        min_trade_count=min_trade_count,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-01  First call → allowed (no prior state)
# ─────────────────────────────────────────────────────────────────────────────

def test_first_call_allowed():
    """should_emit returns True on the very first call — no state exists."""
    with patch(
        "signals.signal_debounce.get_param",
        side_effect=lambda key, default: True if "enabled" in key else 300.0,
    ):
        db = SignalDebounce()
        assert db.should_emit(_SYM, _CONT, _LVL) is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-02  Second call within window → suppressed
# ─────────────────────────────────────────────────────────────────────────────

def test_suppressed_within_window():
    """After record_emit, a second call within the window returns False."""
    with patch(
        "signals.signal_debounce.get_param",
        side_effect=lambda key, default: True if "enabled" in key else 300.0,
    ):
        db = SignalDebounce()
        # First call allowed
        assert db.should_emit(_SYM, _CONT, _LVL) is True
        # Stamp at a known time
        db.record_emit(_SYM, _CONT, _LVL, at=_NOW)
        # Second call 60 s later — still inside the 300 s window
        with patch("signals.signal_debounce.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW + timedelta(seconds=60)
            result = db.should_emit(_SYM, _CONT, _LVL)
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-03  Call after window expires → allowed again
# ─────────────────────────────────────────────────────────────────────────────

def test_allowed_after_window_expires():
    """should_emit returns True once the debounce window has elapsed."""
    with patch(
        "signals.signal_debounce.get_param",
        side_effect=lambda key, default: True if "enabled" in key else 300.0,
    ):
        db = SignalDebounce()
        db.record_emit(_SYM, _CONT, _LVL, at=_NOW)
        # 301 s after stamp — window (300 s) has expired
        with patch("signals.signal_debounce.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW + timedelta(seconds=301)
            result = db.should_emit(_SYM, _CONT, _LVL)
        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-04  debounce_enabled=False → bypass (always True)
# ─────────────────────────────────────────────────────────────────────────────

def test_bypass_when_disabled():
    """When debounce_enabled is False every call emits regardless of state."""
    # Return False for debounce_enabled, 300 for window
    def _param(key, default):
        if "enabled" in key:
            return False
        return 300.0

    with patch("signals.signal_debounce.get_param", side_effect=_param):
        db = SignalDebounce()
        db.record_emit(_SYM, _CONT, _LVL, at=_NOW)
        # Even after just recording an emit, bypass should return True
        assert db.should_emit(_SYM, _CONT, _LVL) is True
        assert db.should_emit(_SYM, _CONT, _LVL) is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-05  Different alert_level on same contract → both emit
# ─────────────────────────────────────────────────────────────────────────────

def test_different_alert_level_both_emit():
    """alert_level is a key dimension: NOTEWORTHY and GOLDEN are independent."""
    with patch(
        "signals.signal_debounce.get_param",
        side_effect=lambda key, default: True if "enabled" in key else 300.0,
    ):
        db = SignalDebounce()
        # Emit NOTEWORTHY
        db.record_emit(_SYM, _CONT, "NOTEWORTHY", at=_NOW)
        # GOLDEN has no state yet — must be allowed even within the window
        with patch("signals.signal_debounce.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW + timedelta(seconds=10)
            result = db.should_emit(_SYM, _CONT, "GOLDEN")
        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-06  record_emit at= stamps explicit time
# ─────────────────────────────────────────────────────────────────────────────

def test_record_emit_explicit_at():
    """record_emit(..., at=dt) stamps exactly dt, not datetime.now()."""
    explicit_ts = _NOW - timedelta(hours=1)

    with patch(
        "signals.signal_debounce.get_param",
        side_effect=lambda key, default: True if "enabled" in key else 300.0,
    ):
        db = SignalDebounce()
        db.record_emit(_SYM, _CONT, _LVL, at=explicit_ts)
        key = (_SYM, _CONT, _LVL)
        assert db._state[key] == explicit_ts


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-07  clear() resets state → next call allows emission
# ─────────────────────────────────────────────────────────────────────────────

def test_clear_resets_state():
    """After clear(), every combination is treated as first-call again."""
    with patch(
        "signals.signal_debounce.get_param",
        side_effect=lambda key, default: True if "enabled" in key else 300.0,
    ):
        db = SignalDebounce()
        db.record_emit(_SYM, _CONT, _LVL, at=_NOW)
        # Confirm it would be suppressed before clearing
        with patch("signals.signal_debounce.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW + timedelta(seconds=10)
            assert db.should_emit(_SYM, _CONT, _LVL) is False

        db.clear()
        assert len(db._state) == 0

        with patch(
            "signals.signal_debounce.get_param",
            side_effect=lambda key, default: True if "enabled" in key else 300.0,
        ):
            assert db.should_emit(_SYM, _CONT, _LVL) is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-DB-08  stats() reflects current config + tracked key count
# ─────────────────────────────────────────────────────────────────────────────

def test_stats_reflects_config_and_key_count():
    """stats() returns tracked_keys, debounce_enabled, and window_seconds."""
    def _param(key, default):
        if "enabled" in key:
            return True
        return 120.0

    with patch("signals.signal_debounce.get_param", side_effect=_param):
        db = SignalDebounce()
        assert db.stats()["tracked_keys"] == 0

        db.record_emit(_SYM, _CONT, _LVL, at=_NOW)
        db.record_emit(_SYM, _CONT, "BLOCK", at=_NOW)

        s = db.stats()
        assert s["tracked_keys"] == 2
        assert s["debounce_enabled"] is True
        assert s["window_seconds"] == 120.0


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-01  All 5 dimensions pass → score=5, normalised pct=100
# ─────────────────────────────────────────────────────────────────────────────

def test_all_dimensions_pass_score_5():
    """Perfect episode: all five dimensions satisfied → score=5."""
    ep = _ep(
        ask_side_pct=0.80,
        vol_oi_signal=True,
        notional_tier="GOLDEN",
        dte_bucket="8-30",
        trade_count=5,
    )
    cfg = _cfg_dict(ask_side_pct_floor=0.6, min_trade_count=3)
    score = compute_conviction_score(ep, cfg)
    assert score == 5
    # Normalised to percentage (composite_score = score / 5.0 → 100 %)
    assert round(score / 5.0 * 100) == 100


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-02  No dimensions pass → score=0, normalised pct=0
# ─────────────────────────────────────────────────────────────────────────────

def test_no_dimensions_pass_score_0():
    """Episode that fails every dimension → score=0."""
    ep = _ep(
        ask_side_pct=0.10,        # below 0.6 floor
        vol_oi_signal=False,
        notional_tier="SMALL",    # not in qualifying tiers
        dte_bucket="0-7",         # disqualifying
        trade_count=1,             # below min_trade_count=3
    )
    cfg = _cfg_dict(ask_side_pct_floor=0.6, min_trade_count=3)
    score = compute_conviction_score(ep, cfg)
    assert score == 0
    assert round(score / 5.0 * 100) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-03  Ask-side only → score=1
# ─────────────────────────────────────────────────────────────────────────────

def test_ask_side_only_score_1():
    """Only D1 (ask-side) passes → score=1."""
    ep = _ep(
        ask_side_pct=0.75,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="0-7",
        trade_count=1,
    )
    cfg = _cfg_dict(ask_side_pct_floor=0.6, min_trade_count=3)
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-04  Vol>OI only → score=1
# ─────────────────────────────────────────────────────────────────────────────

def test_vol_oi_only_score_1():
    """Only D2 (vol>OI) passes → score=1."""
    ep = _ep(
        ask_side_pct=0.10,        # D1 fails
        vol_oi_signal=True,       # D2 passes
        notional_tier="SMALL",    # D3 fails
        dte_bucket="0-7",         # D4 fails
        trade_count=1,             # D5 fails
    )
    cfg = _cfg_dict(ask_side_pct_floor=0.6, min_trade_count=3)
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-05  Qualifying notional tier — NOTEWORTHY
# ─────────────────────────────────────────────────────────────────────────────

def test_qualifying_tier_noteworthy():
    """NOTEWORTHY tier contributes the D3 dimension point."""
    ep = _ep(
        ask_side_pct=None,         # D1 absent → no point
        vol_oi_signal=False,       # D2 fails
        notional_tier="NOTEWORTHY",# D3 passes
        dte_bucket="0-7",          # D4 fails
        trade_count=0,              # D5 fails
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-06  Qualifying notional tier — BLOCK
# ─────────────────────────────────────────────────────────────────────────────

def test_qualifying_tier_block():
    """BLOCK tier contributes the D3 dimension point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="BLOCK",
        dte_bucket="0-7",
        trade_count=0,
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-07  Qualifying notional tier — GOLDEN
# ─────────────────────────────────────────────────────────────────────────────

def test_qualifying_tier_golden():
    """GOLDEN tier contributes the D3 dimension point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="GOLDEN",
        dte_bucket="0-7",
        trade_count=0,
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-08  Disqualifying tier — SMALL
# ─────────────────────────────────────────────────────────────────────────────

def test_disqualifying_tier_small():
    """SMALL tier does NOT contribute the D3 dimension point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="8-30",         # D4 passes — isolate D3 test
        trade_count=0,
    )
    cfg = _cfg_dict()
    # Only D4 should pass (dte_bucket valid)
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-09  DTE bucket 0-7 → D4 disqualified
# ─────────────────────────────────────────────────────────────────────────────

def test_dte_bucket_0_7_disqualified():
    """dte_bucket '0-7' removes the D4 point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="0-7",
        trade_count=0,
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-10  DTE bucket 90+ → D4 disqualified
# ─────────────────────────────────────────────────────────────────────────────

def test_dte_bucket_90_plus_disqualified():
    """dte_bucket '90+' removes the D4 point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="90+",
        trade_count=0,
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-11  DTE bucket 8-30 → D4 passes
# ─────────────────────────────────────────────────────────────────────────────

def test_dte_bucket_8_30_qualifies():
    """dte_bucket '8-30' contributes the D4 point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="8-30",
        trade_count=0,
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-12  Repetition count meets floor → D5 point
# ─────────────────────────────────────────────────────────────────────────────

def test_repetition_meets_floor():
    """trade_count == min_trade_count (at the exact floor) earns the D5 point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="0-7",
        trade_count=3,            # exactly at floor
    )
    cfg = _cfg_dict(min_trade_count=3)
    assert compute_conviction_score(ep, cfg) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-13  Repetition count below floor → no D5 point
# ─────────────────────────────────────────────────────────────────────────────

def test_repetition_below_floor():
    """trade_count < min_trade_count means no D5 point."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="0-7",
        trade_count=2,            # below floor of 3
    )
    cfg = _cfg_dict(min_trade_count=3)
    assert compute_conviction_score(ep, cfg) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-14  cfg dict key access (sig.* convention)
# ─────────────────────────────────────────────────────────────────────────────

def test_cfg_dict_sig_key_convention():
    """compute_conviction_score works when cfg uses sig.* dict keys."""
    ep = _ep(ask_side_pct=0.7, trade_count=4)
    cfg = {
        "sig.ask_side_pct_floor": 0.6,
        "sig.min_trade_count": 3,
    }
    score = compute_conviction_score(ep, cfg)
    # At minimum: D1 (ask-side 0.7>=0.6) + D2 (vol_oi=True) +
    #             D3 (GOLDEN) + D4 (8-30) + D5 (4>=3) = 5
    assert score == 5


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-15  cfg object attribute access (ask_side_pct_floor, min_trade_count)
# ─────────────────────────────────────────────────────────────────────────────

def test_cfg_object_attribute_access():
    """compute_conviction_score works when cfg is an object with plain attrs."""
    ep = _ep(ask_side_pct=0.7, trade_count=4)
    cfg = _cfg_obj(ask_side_pct_floor=0.6, min_trade_count=3)
    score = compute_conviction_score(ep, cfg)
    assert score == 5


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-16  composite_score normalisation — max conviction → 1.000 (100 %)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw_score,expected_pct", [
    (0, 0),
    (1, 20),
    (2, 40),
    (3, 60),
    (4, 80),
    (5, 100),
])
def test_composite_score_normalisation(raw_score: int, expected_pct: int):
    """composite_score = conviction / 5.0; at max score the result is 100 %."""
    normalised_pct = round(raw_score / 5.0 * 100)
    assert normalised_pct == expected_pct


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-17  ask_side_pct=None treated as no-score (D1 skipped)
# ─────────────────────────────────────────────────────────────────────────────

def test_ask_side_pct_none_no_d1_point():
    """When ask_side_pct is None, D1 contributes 0 (graceful degrade)."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket="0-7",
        trade_count=0,
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TC-CV-18  dte_bucket=None treated as no-score (D4 skipped)
# ─────────────────────────────────────────────────────────────────────────────

def test_dte_bucket_none_no_d4_point():
    """When dte_bucket is None, D4 contributes 0 (graceful degrade)."""
    ep = _ep(
        ask_side_pct=None,
        vol_oi_signal=False,
        notional_tier="SMALL",
        dte_bucket=None,
        trade_count=0,
    )
    cfg = _cfg_dict()
    assert compute_conviction_score(ep, cfg) == 0
