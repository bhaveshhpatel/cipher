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
  E-13  Alert level GOLDEN: premium >= 1_000_000 (T1)
  E-14  Alert level BLOCK: premium >= 500K but < 1M
  E-15  Alert level WATCH: noteworthy_premium=0 override so gate passes → WATCH
  E-16  Tier multiplier: T2 NOTEWORTHY threshold scaled to 25K
  E-17  Tier multiplier: T3 NOTEWORTHY threshold scaled to 10K
  E-18  bus listener discards failed episode — no persist_composite_signal call
  E-19  bus listener persists passing episode with EpisodeEvalResult forwarded

  --- Gap tests (AC coverage from issue #107) ---

  E-20  AC-1: T1 GOLDEN — $1M premium, ask_side_pct=1.0, vol>OI, DTE=MID → GOLDEN
  E-21  AC-2: T3 GOLDEN — $200K (exactly 1M×0.2) → GOLDEN
  E-22  AC-2b: T3 $199_999 — below GOLDEN threshold → BLOCK (not GOLDEN)
  E-23  AC-3: T3 BLOCK — $150K (above $100K BLOCK, below $200K GOLDEN) → BLOCK
  E-24  AC-3b: T3 $100K exactly (BLOCK threshold) → BLOCK
  E-25  AC-3c: T3 $99_999 — below BLOCK threshold → NOTEWORTHY (above 10K)
  E-26  T2 GOLDEN — $500K on T2 (1M×0.5) → GOLDEN
  E-27  T2 GOLDEN boundary — $499_999 on T2 → BLOCK
  E-28  T2 BLOCK — $250K on T2 (500K×0.5) → BLOCK
  E-29  T2 BLOCK boundary — $249_999 on T2 → NOTEWORTHY
  E-30  D1 exact floor boundary — T1 $50_000 exactly passes; $49_999 fails
  E-31  D2 ask_side_pct exact boundary — 0.6 passes; 0.5999 fails
  E-32  D5 trade_count exact floor — trade_count=2 passes; trade_count=1 fails
  E-33  All 4 valid DTE buckets pass when config window is widened [1, 90]
  E-34  LONG bucket (midpoint=45) passes default config [5, 60]
  E-35  effective_threshold field populated on both pass and fail results
  E-36  D2+D3 full bypass combo — both require_ask_side=False AND
        require_vol_gt_oi=False simultaneously with worst-case episode

  --- Smoke tests: evaluate() object-based API ---

  S-01  All 5 gates pass — GateResult shape, types, and all-pass assertion
  S-02  D1 fail via evaluate() — gate_1_premium fails, alert_level=None
  S-03  D2 fail via evaluate() — gate_2_ask_side fails
  S-04  D3 fail via evaluate() — gate_3_vol_oi fails (vol_oi_signal=False)
  S-05  D4 fail via evaluate() — gate_4_dte fails (XLONG bucket)
  S-06  D5 fail via evaluate() — gate_5_repetition fails (trade_count=1)
  S-07  Multiple gates fail — steamroom_score reflects actual pass count
  S-08  GOLDEN alert_level via evaluate()
  S-09  WATCH via evaluate() when noteworthy_premium=0 override
  S-10  config_snapshot contains both prefixed and bare key forms

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
  GOLDEN     T2=0.5  T3=0.2
  BLOCK      T2=0.5  T3=0.2
  NOTEWORTHY T2=0.5  T3=0.2
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.signal_engine import EpisodeEvalResult, SignalEngine
from signals.signal_engine import (
    GateResult,
    _EpisodeProxy,
    _GATE_PREMIUM,
    _GATE_ASK_SIDE,
    _GATE_VOL_OI,
    _GATE_DTE,
    _GATE_REPETITION,
    _normalise_tier,
)

# ---------------------------------------------------------------------------
# Stub SignalConfigStore
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "require_ask_side":     True,
    "ask_side_pct_floor":   0.6,
    "require_vol_gt_oi":    True,
    "min_dte":              5,
    "max_dte":              60,
    "min_trade_count":      2,
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
        "ticker":                  "AAPL",
        "total_premium":           75_000,      # above NOTEWORTHY=50K
        "notional_tier":           "T1",
        "ask_side_pct":            0.75,        # above floor=0.6
        "ask_side_count":          3,
        "vol_oi_signal":           True,
        "dte_bucket":              "MID",       # midpoint=19, inside [5, 60]
        "trade_count":             3,           # above min=2
        "direction":               "BULLISH",
        "contract_type":           "CALL",
        "trade_type":              "SWEEP",
        "episode_steamroom_score": 3,
        "is_multi_day_repeat":     False,
    }
    base.update(kwargs)
    return base


def _proxy(**ep_kwargs) -> _EpisodeProxy:
    """Return an _EpisodeProxy wrapping a good episode for evaluate() calls."""
    ep_dict = _good_episode(**ep_kwargs)
    raw_tier = ep_dict.get("notional_tier", "T1")
    return _EpisodeProxy(ep_dict, normalised_tier=_normalise_tier(raw_tier))


# ===========================================================================
# E-01 — E-19  (original suite)
# ===========================================================================

def test_e01_all_pass_noteworthy():
    eng = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=75_000))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"
    assert result.failing_dimensions == []
    assert result.premium == 75_000


def test_e02_d1_premium_fail():
    """D1 gate: premium=49_999 is strictly below noteworthy_threshold=50_000 → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=49_999))
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


def test_e03_d2_ask_side_fail():
    """D2 gate: ask_side_pct=0.4 is below floor=0.6 → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.4))
    assert result.passed is False
    assert "D2_ASK_SIDE" in result.failing_dimensions


def test_e04_d2_ask_side_bypass():
    eng    = _engine({"require_ask_side": False})
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.0))
    assert result.passed is True
    assert "D2_ASK_SIDE" not in result.failing_dimensions


def test_e05_d3_vol_oi_false_fail():
    """D3 gate: vol_oi_signal=False with require_vol_gt_oi=True → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(vol_oi_signal=False))
    assert result.passed is False
    assert "D3_VOL_GT_OI" in result.failing_dimensions


def test_e06_d3_vol_oi_none_fail():
    """D3 gate: vol_oi_signal=None (cache miss) with require_vol_gt_oi=True → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(vol_oi_signal=None))
    assert result.passed is False
    assert "D3_VOL_GT_OI" in result.failing_dimensions


def test_e07_d3_vol_oi_bypass():
    eng    = _engine({"require_vol_gt_oi": False})
    result = eng.evaluate_episode(_good_episode(vol_oi_signal=False))
    assert result.passed is True
    assert "D3_VOL_GT_OI" not in result.failing_dimensions


def test_e08_d4_dte_xlong_fail():
    """D4 gate: XLONG midpoint=75 > max_dte=60 → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket="XLONG"))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


def test_e09_d4_dte_short_fail():
    """D4 gate: SHORT midpoint=4 < min_dte=5 → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket="SHORT"))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


@pytest.mark.parametrize("bucket", [None, "", "UNKNOWN", "EXPIRED"])
def test_e10_d4_dte_unknown_bucket_fail(bucket):
    """D4 gate: unrecognised / None / empty / EXPIRED dte_bucket → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket=bucket))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


def test_e11_d5_repetition_fail():
    """D5 gate: trade_count=1 < min_trade_count=2 → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(trade_count=1))
    assert result.passed is False
    assert "D5_REPETITION" in result.failing_dimensions


def test_e12_multiple_dimensions_fail():
    eng    = _engine()
    ep     = _good_episode(
        ask_side_pct=0.1,
        vol_oi_signal=False,
        trade_count=1,
    )
    result = eng.evaluate_episode(ep)
    assert result.passed is False
    assert "D2_ASK_SIDE"   in result.failing_dimensions
    assert "D3_VOL_GT_OI"  in result.failing_dimensions
    assert "D5_REPETITION" in result.failing_dimensions


def test_e13_alert_level_golden():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=1_000_000))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"


def test_e14_alert_level_block():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=600_000))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


def test_e15_alert_level_watch_floor():
    """WATCH is reachable when noteworthy_premium=0 overrides the floor to zero.

    With noteworthy_premium=0 the D1 hard-floor check short-circuits to
    GateVerdict(True, "premium_cleared_watch (noteworthy=0)") regardless of
    the raw premium value, so a $15K episode passes D1 and resolves to WATCH
    because none of the higher-tier thresholds (GOLDEN=$10M, BLOCK=$9M) are
    met.
    """
    eng = _engine({
        "noteworthy_premium":   0,
        "block_premium":        9_000_000,
        "golden_sweep_premium": 10_000_000,
    })
    result = eng.evaluate_episode(_good_episode(total_premium=15_000))
    assert result.passed is True
    assert result.alert_level == "WATCH"


def test_e16_tier_t2_noteworthy_threshold():
    eng    = _engine()
    result = eng.evaluate_episode(
        _good_episode(total_premium=30_000, notional_tier="T2")
    )
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


def test_e16b_tier_t2_noteworthy_fail_below_scaled_threshold():
    """T2 scaled noteworthy threshold = 25_000; premium=24_999 → D1 fail."""
    eng    = _engine()
    result = eng.evaluate_episode(
        _good_episode(total_premium=24_999, notional_tier="T2")
    )
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


def test_e17_tier_t3_noteworthy_threshold():
    eng    = _engine()
    result = eng.evaluate_episode(
        _good_episode(total_premium=12_000, notional_tier="T3")
    )
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


def test_e17b_tier_t3_noteworthy_fail_below_scaled_threshold():
    """T3 scaled noteworthy threshold = 10_000; premium=9_999 → D1 fail."""
    eng    = _engine()
    result = eng.evaluate_episode(
        _good_episode(total_premium=9_999, notional_tier="T3")
    )
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


@pytest.mark.asyncio
async def test_e18_bus_listener_discards_failed_episode():
    from services import signal_store

    failing_ep  = _good_episode(trade_count=0)
    failing_sig = {"ticker": "AAPL", "composite_score": 0.9}
    bus_msg = {
        "type": "composite_signal",
        "data": {"signal": failing_sig, "episode": failing_ep},
    }

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
        q = MagicMock()
        q.get = AsyncMock(side_effect=[bus_msg, asyncio.CancelledError()])
        with patch.object(signal_store.bus, "subscribe", return_value=q), \
             patch.object(signal_store.bus, "unsubscribe"):
            with pytest.raises(asyncio.CancelledError):
                await signal_store._bus_signal_listener()

    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_e19_bus_listener_persists_passing_episode():
    from services import signal_store

    good_ep  = _good_episode()
    good_sig = {"ticker": "AAPL", "composite_score": 0.9}
    bus_msg  = {
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


# ===========================================================================
# E-20 — E-36  GAP TESTS: Issue #107 AC coverage
# ===========================================================================

def test_e20_ac1_t1_golden_full_description():
    """AC: T1 $1M premium, 3 ask-side prints, vol>OI, DTE=MID → GOLDEN."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        ticker="SPY",
        total_premium=1_000_000,
        notional_tier="T1",
        ask_side_pct=1.0,
        ask_side_count=3,
        vol_oi_signal=True,
        dte_bucket="MID",
        trade_count=3,
    ))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"
    assert result.failing_dimensions == []


def test_e21_ac2_t3_golden_200k_exactly():
    """AC: T3 $200K exactly (1M × 0.2) → GOLDEN."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=200_000,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"


def test_e22_t3_199999_is_block_not_golden():
    """T3 GOLDEN threshold=$200K; $199_999 → BLOCK."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=199_999,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


def test_e23_ac3_t3_block_150k():
    """AC: T3 $150K → BLOCK (above $100K BLOCK, below $200K GOLDEN)."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=150_000,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


def test_e24_t3_block_exact_boundary_100k():
    """T3 BLOCK threshold=$100K exactly → BLOCK."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=100_000,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


def test_e25_t3_99999_is_noteworthy_not_block():
    """T3 $99_999 — one dollar below BLOCK threshold → NOTEWORTHY."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=99_999,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


def test_e26_t2_golden_500k():
    """T2 GOLDEN threshold=$500K (1M × 0.5) exactly → GOLDEN."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=500_000,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"


def test_e27_t2_499999_is_block_not_golden():
    """T2 $499_999 — one dollar below GOLDEN threshold → BLOCK."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=499_999,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


def test_e28_t2_block_250k():
    """T2 BLOCK threshold=$250K (500K × 0.5) exactly → BLOCK."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=250_000,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


def test_e29_t2_249999_is_noteworthy_not_block():
    """T2 $249_999 — one dollar below BLOCK threshold → NOTEWORTHY."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=249_999,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


def test_e30_d1_floor_boundary_passes_at_exactly_50k():
    """D1 gate uses >=: $50_000 exactly must pass."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=50_000))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"
    assert "D1_PREMIUM" not in result.failing_dimensions


def test_e30b_d1_floor_boundary_fails_at_49999():
    """D1 gate: $49_999 is strictly below noteworthy_threshold=50_000 → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=49_999))
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


def test_e31_d2_ask_side_pct_exact_floor_passes():
    """D2 gate uses >=: ask_side_pct=0.6 exactly must pass."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.6))
    assert result.passed is True
    assert "D2_ASK_SIDE" not in result.failing_dimensions


def test_e31b_d2_ask_side_pct_just_below_floor_fails():
    """D2 gate: ask_side_pct=0.5999 is strictly below floor=0.6 → fail."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.5999))
    assert result.passed is False
    assert "D2_ASK_SIDE" in result.failing_dimensions


def test_e32_d5_trade_count_exact_floor_passes():
    """D5 gate uses >=: trade_count=2 exactly must pass."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(trade_count=2))
    assert result.passed is True
    assert "D5_REPETITION" not in result.failing_dimensions


def test_e32b_d5_trade_count_one_below_floor_fails():
    """AC: trade_count=1 with min_trade_count=2 → D5 fail; no signal generated."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(trade_count=1))
    assert result.passed is False
    assert "D5_REPETITION" in result.failing_dimensions


@pytest.mark.parametrize("bucket,midpoint", [
    ("SHORT", 4),
    ("MID",   19),
    ("LONG",  45),
    ("XLONG", 75),
])
def test_e33_all_valid_dte_buckets_pass_with_wide_window(bucket, midpoint):
    """All 4 recognised DTE buckets must pass D4 when window is [1, 90]."""
    eng    = _engine({"min_dte": 1, "max_dte": 90})
    result = eng.evaluate_episode(_good_episode(dte_bucket=bucket))
    assert result.passed is True, (
        f"bucket={bucket!r} (midpoint={midpoint}) should pass D4 with window [1, 90]"
    )
    assert "D4_DTE" not in result.failing_dimensions


def test_e34_long_dte_bucket_passes_default_config():
    """LONG bucket midpoint=45 is inside default [5, 60] — D4 must pass."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket="LONG"))
    assert result.passed is True
    assert "D4_DTE" not in result.failing_dimensions


def test_e35_effective_threshold_populated_on_pass():
    """effective_threshold == T1 noteworthy threshold (50_000) on a passing episode."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=75_000, notional_tier="T1"))
    assert result.effective_threshold == 50_000.0


def test_e35b_effective_threshold_populated_on_t2_pass():
    """effective_threshold == T2 noteworthy threshold (25_000) on a passing T2 episode."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=30_000, notional_tier="T2"))
    assert result.effective_threshold == 25_000.0


def test_e35c_effective_threshold_populated_on_d1_fail():
    """effective_threshold is populated even when D1 fails — needed for logging."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=1_000, notional_tier="T1"))
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions
    assert result.effective_threshold == 50_000.0


def test_e35d_effective_threshold_t3_on_fail():
    """T3 D1 fail — effective_threshold should be 10_000 (50K × 0.2)."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=500, notional_tier="T3"))
    assert result.passed is False
    assert result.effective_threshold == 10_000.0


def test_e36_d2_d3_full_bypass_combo():
    """With both require_* flags disabled, worst-case episode passes on D1/D4/D5."""
    eng    = _engine({
        "require_ask_side":  False,
        "require_vol_gt_oi": False,
    })
    result = eng.evaluate_episode(_good_episode(
        ask_side_pct=0.0,
        vol_oi_signal=None,
    ))
    assert result.passed is True
    assert "D2_ASK_SIDE"  not in result.failing_dimensions
    assert "D3_VOL_GT_OI" not in result.failing_dimensions
    assert result.alert_level == "NOTEWORTHY"


# ===========================================================================
# S-01 — S-10  SMOKE TESTS: evaluate() object-based API
# ===========================================================================

def test_s01_evaluate_all_pass_returns_gate_result():
    """evaluate() returns a GateResult with correct types and all gates passing."""
    eng    = _engine()
    result = eng.evaluate(_proxy())

    assert isinstance(result, GateResult)
    assert result.passed is True
    assert result.steamroom_score == 5
    assert result.alert_level == "NOTEWORTHY"
    assert len(result.gates) == 5
    for gate_name, verdict in result.gates.items():
        assert verdict.passed is True, f"Gate {gate_name!r} should pass but did not"
    assert isinstance(result.config_snapshot, dict)


def test_s02_evaluate_d1_fail_gate_result():
    """D1 fail via evaluate(): gate_1_premium.passed=False, alert_level=None."""
    eng    = _engine()
    result = eng.evaluate(_proxy(total_premium=49_999))

    assert result.passed is False
    assert result.gates[_GATE_PREMIUM].passed is False
    assert result.alert_level is None
    assert result.steamroom_score == 4


def test_s03_evaluate_d2_fail():
    """D2 fail via evaluate(): gate_2_ask_side.passed=False."""
    eng    = _engine()
    result = eng.evaluate(_proxy(ask_side_pct=0.1))

    assert result.passed is False
    assert result.gates[_GATE_ASK_SIDE].passed is False
    assert result.gates[_GATE_PREMIUM].passed is True
    assert result.steamroom_score == 4


def test_s04_evaluate_d3_fail_vol_false():
    """D3 fail via evaluate(): gate_3_vol_oi.passed=False when vol_oi_signal=False."""
    eng    = _engine()
    result = eng.evaluate(_proxy(vol_oi_signal=False))

    assert result.passed is False
    assert result.gates[_GATE_VOL_OI].passed is False
    assert result.steamroom_score == 4


def test_s05_evaluate_d4_fail_xlong():
    """D4 fail via evaluate(): XLONG midpoint=75 > max_dte=60."""
    eng    = _engine()
    result = eng.evaluate(_proxy(dte_bucket="XLONG"))

    assert result.passed is False
    assert result.gates[_GATE_DTE].passed is False
    assert result.steamroom_score == 4


def test_s06_evaluate_d5_fail_trade_count():
    """D5 fail via evaluate(): trade_count=1 < min_trade_count=2."""
    eng    = _engine()
    result = eng.evaluate(_proxy(trade_count=1))

    assert result.passed is False
    assert result.gates[_GATE_REPETITION].passed is False
    assert result.steamroom_score == 4


def test_s07_evaluate_multiple_gates_fail():
    """D2 + D3 + D5 fail simultaneously; steamroom_score=2 (D1 + D4 pass)."""
    eng    = _engine()
    result = eng.evaluate(_proxy(ask_side_pct=0.1, vol_oi_signal=False, trade_count=1))

    assert result.passed is False
    assert result.gates[_GATE_PREMIUM].passed    is True
    assert result.gates[_GATE_ASK_SIDE].passed   is False
    assert result.gates[_GATE_VOL_OI].passed     is False
    assert result.gates[_GATE_DTE].passed        is True
    assert result.gates[_GATE_REPETITION].passed is False
    assert result.steamroom_score == 2


def test_s08_evaluate_golden_alert_level():
    """All gates pass with $1M premium → alert_level=GOLDEN."""
    eng    = _engine()
    result = eng.evaluate(_proxy(total_premium=1_000_000))

    assert result.passed is True
    assert result.alert_level == "GOLDEN"
    assert result.steamroom_score == 5


def test_s09_evaluate_watch_when_noteworthy_zero():
    """WATCH via evaluate(): noteworthy_premium=0 forces gate-1 WATCH branch."""
    eng = _engine({
        "noteworthy_premium":   0,
        "block_premium":        9_000_000,
        "golden_sweep_premium": 10_000_000,
    })
    ep_dict = _good_episode(total_premium=15_000)
    proxy   = _EpisodeProxy(ep_dict, normalised_tier="T1")
    result  = eng.evaluate(proxy)

    assert result.passed is True
    assert result.alert_level == "WATCH"
    assert result.gates[_GATE_PREMIUM].passed is True


def test_s10_evaluate_config_snapshot_contains_keys():
    """config_snapshot must expose both prefixed and bare forms of all keys."""
    eng    = _engine()
    result = eng.evaluate(_proxy())
    snap   = result.config_snapshot

    assert "require_ask_side"     in snap
    assert "sig.require_ask_side" in snap
    assert "noteworthy_premium"   in snap
    assert snap["noteworthy_premium"]       == 50_000
    assert snap["sig.noteworthy_premium"]   == 50_000
