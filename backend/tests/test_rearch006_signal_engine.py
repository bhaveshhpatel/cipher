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
  E-15  Alert level WATCH: premium at floor (below NOTEWORTHY label)
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
        "episode_steamroom_score": 3,           # used for conviction tests
        "is_multi_day_repeat":     False,
    }
    base.update(kwargs)
    return base


# ===========================================================================
# E-01 — E-19  (original suite — unchanged)
# ===========================================================================

def test_e01_all_pass_noteworthy():
    eng = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=75_000))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"
    assert result.failing_dimensions == []
    assert result.premium == 75_000


def test_e02_d1_premium_fail():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=49_999))
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


def test_e03_d2_ask_side_fail():
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
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(vol_oi_signal=False))
    assert result.passed is False
    assert "D3_VOL_GT_OI" in result.failing_dimensions


def test_e06_d3_vol_oi_none_fail():
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
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket="XLONG"))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


def test_e09_d4_dte_short_fail():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket="SHORT"))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


@pytest.mark.parametrize("bucket", [None, "", "UNKNOWN", "EXPIRED"])
def test_e10_d4_dte_unknown_bucket_fail(bucket):
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket=bucket))
    assert result.passed is False
    assert "D4_DTE" in result.failing_dimensions


def test_e11_d5_repetition_fail():
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
    eng = _engine({
        "noteworthy_premium":   10_000,
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

# ---------------------------------------------------------------------------
# E-20: AC-1 — T1 GOLDEN: full Steamroom description match
#   $1M premium, ask_side_pct=1.0 (3 ask-side prints), vol>OI, DTE=MID (~15d)
#   Must produce alert_level=GOLDEN with all 5 gates passing.
# ---------------------------------------------------------------------------

def test_e20_ac1_t1_golden_full_description():
    """
    AC: A GOLDEN episode on a Tier-1 name (premium $1M+, 3 ask-side prints,
    vol>OI, DTE 15) generates alert='GOLDEN'.
    ask_side_pct=1.0 models 3/3 prints at ask; MID bucket midpoint=19 is
    the closest bucket to DTE=15 within config window [5, 60].
    """
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        ticker="SPY",
        total_premium=1_000_000,
        notional_tier="T1",
        ask_side_pct=1.0,       # 3 ask-side prints out of 3
        ask_side_count=3,
        vol_oi_signal=True,
        dte_bucket="MID",       # midpoint=19 ≈ DTE 15, inside [5, 60]
        trade_count=3,
    ))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"
    assert result.failing_dimensions == []


# ---------------------------------------------------------------------------
# E-21: AC-2 — T3 GOLDEN at exactly $200K (1_000_000 × 0.2)
#   The $200K T3 GOLDEN AC is specifically called out in the issue as a
#   key spec item. Must produce GOLDEN, not BLOCK.
# ---------------------------------------------------------------------------

def test_e21_ac2_t3_golden_200k_exactly():
    """
    AC: A GOLDEN episode on a Tier-3 name (premium $200K+) also generates
    alert='GOLDEN' via tier multiplier (1M × 0.2 = 200K threshold).
    """
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=200_000,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"


# ---------------------------------------------------------------------------
# E-22: T3 $199_999 — one dollar below GOLDEN threshold → BLOCK
#   Confirms the GOLDEN/BLOCK boundary is a hard >= comparison.
# ---------------------------------------------------------------------------

def test_e22_t3_199999_is_block_not_golden():
    """
    T3 GOLDEN threshold = $200K. $199_999 must resolve to BLOCK,
    not GOLDEN. Verifies the boundary is strict >=.
    """
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=199_999,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


# ---------------------------------------------------------------------------
# E-23: AC-3 — T3 BLOCK at $150K
#   $150K is above $100K (T3 BLOCK threshold = 500K × 0.2) and below
#   $200K (T3 GOLDEN threshold = 1M × 0.2). Must resolve to BLOCK.
# ---------------------------------------------------------------------------

def test_e23_ac3_t3_block_150k():
    """
    AC: A Tier-3 episode with $150K total_premium generates alert='BLOCK'
    (above $100K BLOCK threshold, below $200K GOLDEN threshold).
    """
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=150_000,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


# ---------------------------------------------------------------------------
# E-24: T3 BLOCK exact lower boundary — $100_000 exactly
#   500_000 × 0.2 = 100_000. At exactly threshold must be BLOCK.
# ---------------------------------------------------------------------------

def test_e24_t3_block_exact_boundary_100k():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=100_000,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


# ---------------------------------------------------------------------------
# E-25: T3 $99_999 — one dollar below BLOCK threshold → NOTEWORTHY
#   Confirms BLOCK/NOTEWORTHY boundary for T3.
# ---------------------------------------------------------------------------

def test_e25_t3_99999_is_noteworthy_not_block():
    """
    T3 BLOCK threshold = $100K. $99_999 must resolve to NOTEWORTHY
    (above T3 NOTEWORTHY threshold of $10K = 50K × 0.2).
    """
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=99_999,
        notional_tier="T3",
    ))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


# ---------------------------------------------------------------------------
# E-26: T2 GOLDEN — $500K on T2 (1_000_000 × 0.5 = 500_000)
# ---------------------------------------------------------------------------

def test_e26_t2_golden_500k():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=500_000,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "GOLDEN"


# ---------------------------------------------------------------------------
# E-27: T2 $499_999 — one dollar below T2 GOLDEN threshold → BLOCK
# ---------------------------------------------------------------------------

def test_e27_t2_499999_is_block_not_golden():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=499_999,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


# ---------------------------------------------------------------------------
# E-28: T2 BLOCK — $250K on T2 (500_000 × 0.5 = 250_000)
# ---------------------------------------------------------------------------

def test_e28_t2_block_250k():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=250_000,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "BLOCK"


# ---------------------------------------------------------------------------
# E-29: T2 $249_999 — one dollar below T2 BLOCK threshold → NOTEWORTHY
# ---------------------------------------------------------------------------

def test_e29_t2_249999_is_noteworthy_not_block():
    """
    T2 BLOCK threshold = $250K. $249_999 must resolve to NOTEWORTHY
    (above T2 NOTEWORTHY threshold of $25K = 50K × 0.5).
    """
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(
        total_premium=249_999,
        notional_tier="T2",
    ))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"


# ---------------------------------------------------------------------------
# E-30: D1 exact floor boundary (T1)
#   $50_000 exactly passes D1; $49_999 fails D1.
#   Verifies the gate uses >= not >.
# ---------------------------------------------------------------------------

def test_e30_d1_floor_boundary_passes_at_exactly_50k():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=50_000))
    assert result.passed is True
    assert result.alert_level == "NOTEWORTHY"
    assert "D1_PREMIUM" not in result.failing_dimensions


def test_e30b_d1_floor_boundary_fails_at_49999():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=49_999))
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions


# ---------------------------------------------------------------------------
# E-31: D2 ask_side_pct exact boundary
#   0.6 exactly passes (floor=0.6, gate is >=); 0.5999 fails.
# ---------------------------------------------------------------------------

def test_e31_d2_ask_side_pct_exact_floor_passes():
    eng    = _engine()  # ask_side_pct_floor=0.6
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.6))
    assert result.passed is True
    assert "D2_ASK_SIDE" not in result.failing_dimensions


def test_e31b_d2_ask_side_pct_just_below_floor_fails():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(ask_side_pct=0.5999))
    assert result.passed is False
    assert "D2_ASK_SIDE" in result.failing_dimensions


# ---------------------------------------------------------------------------
# E-32: D5 trade_count exact floor
#   trade_count=2 passes (min=2, gate is >=); trade_count=1 fails.
# ---------------------------------------------------------------------------

def test_e32_d5_trade_count_exact_floor_passes():
    eng    = _engine()  # min_trade_count=2
    result = eng.evaluate_episode(_good_episode(trade_count=2))
    assert result.passed is True
    assert "D5_REPETITION" not in result.failing_dimensions


def test_e32b_d5_trade_count_one_below_floor_fails():
    """AC: An episode with trade_count=1 never generates a signal when min_trade_count=2."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(trade_count=1))
    assert result.passed is False
    assert "D5_REPETITION" in result.failing_dimensions


# ---------------------------------------------------------------------------
# E-33: All 4 valid DTE buckets pass when config window is widened to [1, 90]
#   SHORT midpoint=4, MID=19, LONG=45, XLONG=75 — all inside [1, 90].
#   Verifies the bucket→midpoint mapping for every recognised bucket.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bucket,midpoint", [
    ("SHORT", 4),
    ("MID",   19),
    ("LONG",  45),
    ("XLONG", 75),
])
def test_e33_all_valid_dte_buckets_pass_with_wide_window(bucket, midpoint):
    """All 4 recognised DTE buckets must pass D4 when config window covers all midpoints."""
    eng    = _engine({"min_dte": 1, "max_dte": 90})
    result = eng.evaluate_episode(_good_episode(dte_bucket=bucket))
    assert result.passed is True, (
        f"bucket={bucket!r} (midpoint={midpoint}) should pass D4 with window [1, 90]"
    )
    assert "D4_DTE" not in result.failing_dimensions


# ---------------------------------------------------------------------------
# E-34: LONG bucket (midpoint=45) passes default config [5, 60]
#   This specific bucket was not covered by E-01 through E-19 (those used MID).
# ---------------------------------------------------------------------------

def test_e34_long_dte_bucket_passes_default_config():
    """
    LONG bucket midpoint=45. Default config: min_dte=5, max_dte=60.
    45 is inside [5, 60] — D4 must pass.
    """
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(dte_bucket="LONG"))
    assert result.passed is True
    assert "D4_DTE" not in result.failing_dimensions


# ---------------------------------------------------------------------------
# E-35: effective_threshold field is populated on both pass and fail results
#   On pass: effective_threshold == noteworthy threshold for the tier.
#   On fail (D1): effective_threshold still reflects the threshold that was
#   checked so callers can log "premium X failed threshold Y".
# ---------------------------------------------------------------------------

def test_e35_effective_threshold_populated_on_pass():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=75_000, notional_tier="T1"))
    # T1 noteworthy threshold = 50_000 (no multiplier)
    assert result.effective_threshold == 50_000.0


def test_e35b_effective_threshold_populated_on_t2_pass():
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=30_000, notional_tier="T2"))
    # T2 noteworthy threshold = 50_000 × 0.5 = 25_000
    assert result.effective_threshold == 25_000.0


def test_e35c_effective_threshold_populated_on_d1_fail():
    """effective_threshold must be set even when D1 fails — callers need it for logging."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=1_000, notional_tier="T1"))
    assert result.passed is False
    assert "D1_PREMIUM" in result.failing_dimensions
    # threshold is still reported so caller can log "premium=1000 < threshold=50000"
    assert result.effective_threshold == 50_000.0


def test_e35d_effective_threshold_t3_on_fail():
    """T3 D1 fail — effective_threshold should be 10_000 (50K × 0.2)."""
    eng    = _engine()
    result = eng.evaluate_episode(_good_episode(total_premium=500, notional_tier="T3"))
    assert result.passed is False
    assert result.effective_threshold == 10_000.0


# ---------------------------------------------------------------------------
# E-36: Full D2+D3 bypass combo
#   require_ask_side=False AND require_vol_gt_oi=False simultaneously.
#   Worst-case episode: ask_side_pct=0.0 and vol_oi_signal=None.
#   Neither D2 nor D3 should appear in failing_dimensions — episode passes
#   on D1 / D4 / D5 alone.
#   AC: "Changing sig.require_ask_side=false via admin PATCH allows bid-side
#   episodes to signal" — extended here to also cover simultaneous vol bypass.
# ---------------------------------------------------------------------------

def test_e36_d2_d3_full_bypass_combo():
    """
    When both require_ask_side and require_vol_gt_oi are disabled, an episode
    with ask_side_pct=0.0 and vol_oi_signal=None must still pass on the
    remaining three dimensions (D1, D4, D5).
    """
    eng    = _engine({
        "require_ask_side":  False,
        "require_vol_gt_oi": False,
    })
    result = eng.evaluate_episode(_good_episode(
        ask_side_pct=0.0,       # worst-case: fully bid-side
        vol_oi_signal=None,     # worst-case: cache miss / no vol>OI data
    ))
    assert result.passed is True
    assert "D2_ASK_SIDE"  not in result.failing_dimensions
    assert "D3_VOL_GT_OI" not in result.failing_dimensions
    assert result.alert_level == "NOTEWORTHY"
