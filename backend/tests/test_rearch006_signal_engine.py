"""
test_rearch006_signal_engine.py — REARCH-006 Signal Engine Tests

Covers:
  E-01  All 5 dimensions pass — NOTEWORTHY alert level
  E-02  D1 fail: premium below NOTEWORTHY threshold
  E-03  D2 fail: ask_side_pct below floor (require_ask_side=true)
  E-04  D2 bypass: require_ask_side=false skips gate regardless of pct
  E-05  D3 fail: vol_oi_signal=False (require_vol_gt_oi=true)
  E-06  D3 fail: vol_oi_signal=None (cache miss, require_vol_gt_oi=true)
  E-07  D3 bypass: require_vol_gt_oi=false skips gate
  E-08  D4 fail: dte_bucket=XLONG outside max_dte=60
  E-09  D4 fail: dte_bucket=SHORT below min_dte=5
  E-10  D4 fail: dte_bucket=None / unrecognised
  E-11  D5 fail: trade_count below min_trade_count
  E-12  Multiple dimensions fail — all reported in failing_dimensions
  E-13  Alert level GOLDEN: premium >= golden tier threshold
  E-14  Alert level BLOCK: premium >= block threshold but < golden
  E-15  Alert level WATCH: premium at floor (below NOTEWORTHY base)
         with custom low noteworthy_premium config
  E-16  Tier multiplier: T2 episode resolves correct scaled threshold
  E-17  Tier multiplier: T3 episode resolves correct scaled threshold
  E-18  signal_store._bus_signal_listener discards failed episode —
         no persist_composite_signal call
  E-19  signal_store._bus_signal_listener persists passing episode and
         forwards EpisodeEvalResult to persist_composite_signal

Test isolation: all tests use a stub SignalConfigStore that returns
pre-seeded config dicts without any DB or network I/O.

Stub config defaults (mirrors migration 030 seeds):
  require_ask_side       = True
  ask_side_pct_floor     = 0.6
  require_vol_gt_oi      = True
  min_dte                = 5
  max_dte                = 60
  min_trade_count        = 2
  golden_sweep_premium   = 1_000_000
  block_premium          = 500_000
  noteworthy_premium     = 50_000

Tier multipliers (mirrors migration 031 seeds):
  GOLDEN   T2=0.5  T3=0.2
  BLOCK    T2=0.5  T3=0.2
  NOTEWORTHY T2=0.5 T3=0.2
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.signal_engine import EpisodeEvalResult, SignalEngine

# ---------------------------------------------------------------------------
# Stub SignalConfigStore
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "require_ask_side":    True,
    "ask_side_pct_floor":  0.6,
    "require_vol_gt_oi":   True,
    "min_dte":             5,
    "max_dte":             60,
    "min_trade_count":     2,
    "golden_sweep_premium": 1_000_000,
    "block_premium":        500_000,
    "noteworthy_premium":   50_000,
}

# Tier multipliers — same structure as SignalConfigStore.get_effective_premium_threshold()
_TIER_MULTIPLIERS = {
    "golden_sweep_premium": {"T2": 0.5, "T3": 0.2},
    "block_premium":        {"T2": 0.5, "T3": 0.2},
    "noteworthy_premium":   {"T2": 0.5, "T3": 0.2},
}


class StubConfigStore:
    """Lightweight stub — no DB, no TTL, config mutated per-test via override dict."""

    def __init__(self, overrides: dict | None = None):
        self._cfg = {**_BASE_CONFIG, **(overrides or {})}

    def get_all(self) -> dict:
        return dict(self._cfg)

    def get_effective_premium_threshold(
        self, alert_level_key: str, notional_tier: str
    ) -> float | None:
        base = self._cfg.get(alert_level_key)
        if base is None:
            return None
        mults = _TIER_MULTIPLIERS.get(alert_level_key, {})
        mult  = mults.get(notional_tier, 1.0)  # T1 has no multiplier — uses base
        return float(base) * mult


def _engine(overrides: dict | None = None) -> SignalEngine:
    """Return a SignalEngine backed by the stub config store."""
    return SignalEngine(StubConfigStore(overrides))


# ---------------------------------------------------------------------------
# Fixture: a valid passing episode (all dimensions satisfied with defaults)
# ---------------------------------------------------------------------------

def _good_episode(**kwargs) -> dict:
    base = {
        "ticker":          "AAPL",
        "total_premium":   75_000,      # above NOTEWORTHY=50K
        "notional_tier":   "T1",
        "ask_side_pct":    0.75,         # above floor=0.6
        "ask_side_count":  3,
        "vol_oi_signal":   True,
        "dte_bucket":      "MID",        # midpoint=19, inside [5, 60]
        "trade_count":     3,            # above min=2
        "direction":       "BULLISH",
        "contract_type":   "CALL",
        "trade_type":      "SWEEP",
    }
    base.update(kwargs)
    return base


# ===========================================================================
# E-01: Full pass — NOTEWORTHY
# ===========================================================================

def test_e01_all_pass_noteworthy():
    eng = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=75_000))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"
    assert result.failing_dimensions == []
    assert result.premium == 75_000


# ===========================================================================
# E-02: D1 fail — premium below NOTEWORTHY (T1 threshold=50K)
# ===========================================================================

def test_e02_d1_premium_fail():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=49_999))
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


# ===========================================================================
# E-03: D2 fail — ask_side_pct below floor
# ===========================================================================

def test_e03_d2_ask_side_fail():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.4))
    assert result.passed is False
    assert "D2_ASK_SIDE" in result.failing_dimensions


# ===========================================================================
# E-04: D2 bypass — require_ask_side=False
# ===========================================================================

def test_e04_d2_ask_side_bypass():
    eng    = _engine({"require_ask_side": False})
    # ask_side_pct is 0.0 — would fail if gate were active
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.0))
    assert result.passed is True
    assert "D2_ASK_SIDE" not in result.failing_dimensions


# ===========================================================================
# E-05: D3 fail — vol_oi_signal=False
# ===========================================================================

def test_e05_d3_vol_oi_false_fail():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(vol_oi_signal=False))
    assert result.passed is False
    assert "D3_VOL_GT_OI" in result.failing_dimensions


# ===========================================================================
# E-06: D3 fail — vol_oi_signal=None (cache miss)
# ===========================================================================

def test_e06_d3_vol_oi_none_fail():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(vol_oi_signal=None))
    assert result.passed is False
    assert "D3_VOL_GT_OI" in result.failing_dimensions


# ===========================================================================
# E-07: D3 bypass — require_vol_gt_oi=False
# ===========================================================================

def test_e07_d3_vol_oi_bypass():
    eng    = _engine({"require_vol_gt_oi": False})
    result = eng.evaluate_episode(_good_episode(vol_oi_signal=False))
    assert result.passed is True
    assert "D3_VOL_GT_OI" not in result.failing_dimensions


# ===========================================================================
# E-08: D4 fail — XLONG bucket midpoint=75 outside max_dte=60
# ===========================================================================

def test_e08_d4_dte_xlong_fail():
    eng    = _engine()  # max_dte=60
    result = eng.evaluate_episode(_good_episode(dte_bucket="XLONG"))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


# ===========================================================================
# E-09: D4 fail — SHORT bucket midpoint=4 below min_dte=5
# ===========================================================================

def test_e09_d4_dte_short_fail():
    eng    = _engine()  # min_dte=5
    result = eng.evaluate_episode(_good_episode(dte_bucket="SHORT"))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


# ===========================================================================
# E-10: D4 fail — unrecognised / None bucket
# ===========================================================================

@pytest.mark.parametrize("bucket", [None, "", "UNKNOWN", "EXPIRED"])
def test_e10_d4_dte_unknown_bucket_fail(bucket):
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket=bucket))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


# ===========================================================================
# E-11: D5 fail — trade_count below min_trade_count
# ===========================================================================

def test_e11_d5_repetition_fail():
    eng    = _engine()  # min_trade_count=2
    result = eng.evaluate_episode(_good_episode(trade_count=1))
    assert result.passed is False
    assert "D5_REPETITION" in result.failing_dimensions


# ===========================================================================
# E-12: Multiple dimension failures — all reported
# ===========================================================================

def test_e12_multiple_dimensions_fail():
    eng    = _engine()
    ep     = _good_episode(
        ask_side_pct=0.1,     # D2 fail
        vol_oi_signal=False,  # D3 fail
        trade_count=1,        # D5 fail
    )
    result = eng.evaluate_episode(ep)
    assert result.passed is False
    assert "D2_ASK_SIDE"   in result.failing_dimensions
    assert "D3_VOL_GT_OI"  in result.failing_dimensions
    assert "D5_REPETITION" in result.failing_dimensions


# ===========================================================================
# E-13: Alert level GOLDEN — premium >= 1_000_000 (T1)
# ===========================================================================

def test_e13_alert_level_golden():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=1_000_000))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"


# ===========================================================================
# E-14: Alert level BLOCK — premium >= 500K but < 1M
# ===========================================================================

def test_e14_alert_level_block():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=600_000))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


# ===========================================================================
# E-15: Alert level WATCH floor
#  Set noteworthy_premium to a very low value so WATCH is reachable via D1
# ===========================================================================

def test_e15_alert_level_watch_floor():
    # Set all premium thresholds above the episode premium except the D1 floor.
    # noteworthy_premium=10K (D1 gate floor), but NOTEWORTHY label is at 9M
    # so the episode lands on WATCH.
    eng = _engine({
        "noteworthy_premium":   10_000,
        "block_premium":        9_000_000,
        "golden_sweep_premium": 10_000_000,
    })
    # total_premium=15K — clears D1 floor (10K) but below all label thresholds
    result = eng.evaluate_episode(_good_episode(total_premium=15_000))
    assert result.passed is True
    assert result.alert_level == "WATCH"


# ===========================================================================
# E-16: Tier multiplier T2 — noteworthy threshold scaled to 25K
# ===========================================================================

def test_e16_tier_t2_noteworthy_threshold():
    eng    = _engine()  # noteworthy_premium=50K, T2 mult=0.5 -> threshold=25K
    result = eng.evaluate_episode(
        _good_episode(total_premium=30_000, notional_tier="T2")
    )
    # 30K >= 25K — D1 should pass
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


def test_e16b_tier_t2_noteworthy_fail_below_scaled_threshold():
    eng    = _engine()  # noteworthy_premium=50K, T2 mult=0.5 -> threshold=25K
    result = eng.evaluate_episode(
        _good_episode(total_premium=24_999, notional_tier="T2")
    )
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


# ===========================================================================
# E-17: Tier multiplier T3 — noteworthy threshold scaled to 10K
# ===========================================================================

def test_e17_tier_t3_noteworthy_threshold():
    eng    = _engine()  # noteworthy_premium=50K, T3 mult=0.2 -> threshold=10K
    result = eng.evaluate_episode(
        _good_episode(total_premium=12_000, notional_tier="T3")
    )
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


def test_e17b_tier_t3_noteworthy_fail_below_scaled_threshold():
    eng    = _engine()  # T3 threshold=10K
    result = eng.evaluate_episode(
        _good_episode(total_premium=9_999, notional_tier="T3")
    )
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


# ===========================================================================
# E-18: bus listener discards failed episode — no persist call
# ===========================================================================

@pytest.mark.asyncio
async def test_e18_bus_listener_discards_failed_episode():
    """
    When the engine returns passed=False, _bus_signal_listener must not call
    persist_composite_signal.
    """
    from services import signal_store

    # Build a failing episode (trade_count=0)
    failing_ep = _good_episode(trade_count=0)
    failing_sig = {"ticker": "AAPL", "composite_score": 0.9}

    bus_msg = {
        "type": "composite_signal",
        "data": {"signal": failing_sig, "episode": failing_ep},
    }

    # Inject a stub engine that always fails
    stub_engine = MagicMock()
    stub_engine.evaluate_episode.return_value = EpisodeEvalResult(
        passed=False,
        alert_level="FAIL",
        failing_dimensions=["D5_REPETITION"],
        premium=75_000,
        ticker="AAPL",
    )

    with patch("services.signal_store.get_engine", return_value=stub_engine), \
         patch("services.signal_store.persist_composite_signal", new_callable=AsyncMock) as mock_persist:

        # Simulate one message on the bus then cancel
        q = MagicMock()
        q.get = AsyncMock(side_effect=[bus_msg, asyncio.CancelledError()])

        with patch.object(signal_store.bus, "subscribe", return_value=q), \
             patch.object(signal_store.bus, "unsubscribe"):
            with pytest.raises(asyncio.CancelledError):
                await signal_store._bus_signal_listener()

    mock_persist.assert_not_called()


# ===========================================================================
# E-19: bus listener persists passing episode with EpisodeEvalResult forwarded
# ===========================================================================

@pytest.mark.asyncio
async def test_e19_bus_listener_persists_passing_episode():
    """
    When the engine returns passed=True, _bus_signal_listener must call
    persist_composite_signal with the correct sig, ep, and eval_result.
    """
    from services import signal_store

    good_ep  = _good_episode()
    good_sig = {"ticker": "AAPL", "composite_score": 0.9}

    bus_msg = {
        "type": "composite_signal",
        "data": {"signal": good_sig, "episode": good_ep},
    }

    pass_result = EpisodeEvalResult(
        passed=True,
        alert_level="NOTEWORTHY",
        failing_dimensions=[],
        premium=75_000,
        ticker="AAPL",
    )

    stub_engine = MagicMock()
    stub_engine.evaluate_episode.return_value = pass_result

    with patch("services.signal_store.get_engine", return_value=stub_engine), \
         patch("services.signal_store.persist_composite_signal", new_callable=AsyncMock) as mock_persist:

        q = MagicMock()
        q.get = AsyncMock(side_effect=[bus_msg, asyncio.CancelledError()])

        with patch.object(signal_store.bus, "subscribe", return_value=q), \
             patch.object(signal_store.bus, "unsubscribe"):
            with pytest.raises(asyncio.CancelledError):
                await signal_store._bus_signal_listener()

    mock_persist.assert_called_once_with(good_sig, good_ep, eval_result=pass_result)
