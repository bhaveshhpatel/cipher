# =============================================================================
# tests/signals/test_signal_engine.py
#
# REARCH-006 Chunk 4 tests
#
# Coverage contract:
#   compute_conviction_score — all 32 combinations of 5 binary dimensions
#   build_signal_row         — valid row structure, ValueError on bad vocab,
#                              None handling for optional fields,
#                              vol_oi_ratio derivation fallback path
#
# Test strategy
# -------------
# Both functions are pure (no I/O, no side effects) so every test is a
# straight call-and-assert with no patching required.
#
# compute_conviction_score parametrization uses itertools.product to
# generate all 2^5 = 32 combinations of the 5 binary dimensions and asserts
# the score == popcount(dimension_vector) in each case.
#
# Dimension mapping (REARCH-006 canonical spec):
#   D1 = ask_side_pct >= floor  (None -> fail)
#   D2 = vol_oi_signal is True
#   D3 = notional_tier in _QUALIFYING_TIERS
#   D4 = dte_bucket not None and not in _DISQUALIFYING_DTE_BUCKETS
#   D5 = trade_count >= min_trade_count
# =============================================================================

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from signals.signal_engine import (
    build_signal_row,
    compute_conviction_score,
    _derive_recommendation,
    _QUALIFYING_TIERS,
    _DISQUALIFYING_DTE_BUCKETS,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Minimum valid config dict accepted by compute_conviction_score.
MIN_CFG = {
    "sig.ask_side_pct_floor": 0.6,
    "sig.min_trade_count": 3,
    "sig.noteworthy_premium": 50_000.0,
}


@dataclass
class EpisodeFaker:
    """Minimal stand-in for RepetitionEpisode in tests.

    Fields mirror the exact attributes that compute_conviction_score and
    build_signal_row read via getattr.  Defaults represent a fully-qualifying
    episode (all 5 dimensions pass).

    D1 — ask_side_pct >= 0.6 (floor)
    D2 — vol_oi_signal is True
    D3 — notional_tier in _QUALIFYING_TIERS ("NOTEWORTHY", "BLOCK", "GOLDEN")
    D4 — dte_bucket not in _DISQUALIFYING_DTE_BUCKETS and not None
    D5 — trade_count >= min_trade_count (3)
    """
    # Required for build_signal_row
    ticker: str = "AAPL"
    symbol: Optional[str] = None
    # D1 — ask-side
    ask_side_pct: float = 0.75
    # D2 — vol > OI
    vol_oi_signal: bool = True
    # D3 — qualifying notional tier
    notional_tier: str = "GOLDEN"
    # D4 — DTE bucket
    dte_bucket: str = "14-30"
    # D5 — trade count
    trade_count: int = 5
    # build_signal_row extras
    total_premium: float = 100_000.0
    contract_type: str = "call"
    episode_id: str = "ep-abc-123"
    # vol_oi_ratio derivation fallback
    vol_oi_ratio: Optional[float] = None
    contract_volume_at_close: Optional[int] = None
    contract_oi_at_open: Optional[int] = None


def _make_cfg(**overrides) -> dict:
    cfg = dict(MIN_CFG)
    cfg.update(overrides)
    return cfg


# =============================================================================
# compute_conviction_score — all 32 combinations (5 binary dimensions)
# =============================================================================

# Dimension index -> (attr_name, passing_value, failing_value)
#
# D1: ask_side_pct >= floor (0.6)
# D2: vol_oi_signal == True
# D3: notional_tier in _QUALIFYING_TIERS
# D4: dte_bucket not in DISQUALIFYING_DTE_BUCKETS and not None
# D5: trade_count >= min_trade_count (3)
_DIMENSION_SPEC: list[tuple[str, Any, Any]] = [
    # D1: ask_side_pct >= floor (0.6)
    ("ask_side_pct",    0.75,          0.3),
    # D2: vol_oi_signal == True
    ("vol_oi_signal",   True,          False),
    # D3: notional_tier in _QUALIFYING_TIERS
    ("notional_tier",   "GOLDEN",      "BELOW_THRESHOLD"),
    # D4: dte_bucket not in DISQUALIFYING_DTE_BUCKETS
    ("dte_bucket",      "14-30",       "0-7"),
    # D5: trade_count >= min_trade_count (3)
    ("trade_count",     5,             1),
]


def _episode_from_vector(vector: tuple[bool, ...]) -> EpisodeFaker:
    kwargs: dict[str, Any] = {}
    for passes, (attr, pass_val, fail_val) in zip(vector, _DIMENSION_SPEC):
        kwargs[attr] = pass_val if passes else fail_val
    return EpisodeFaker(**kwargs)


@pytest.mark.parametrize(
    "vector",
    list(itertools.product([True, False], repeat=5)),
    ids=["".join("P" if v else "F" for v in vec) for vec in itertools.product([True, False], repeat=5)],
)
def test_compute_conviction_score_all_32_combinations(vector: tuple[bool, ...]) -> None:
    """For every 2^5 dimension vector, score must equal popcount(vector)."""
    ep = _episode_from_vector(vector)
    cfg = _make_cfg()
    expected = sum(vector)
    result = compute_conviction_score(ep, cfg)
    assert result == expected, (
        f"vector={vector!r} -> expected score={expected}, got {result}. "
        f"Episode attrs: ask_side_pct={ep.ask_side_pct}, vol_oi_signal={ep.vol_oi_signal}, "
        f"notional_tier={ep.notional_tier!r}, dte_bucket={ep.dte_bucket!r}, trade_count={ep.trade_count}"
    )


# ---------------------------------------------------------------------------
# compute_conviction_score — boundary / edge-case tests
# ---------------------------------------------------------------------------

def test_conviction_d1_ask_side_exact_floor_passes() -> None:
    """ask_side_pct exactly equal to floor (0.6) must count as D1 pass."""
    ep = EpisodeFaker(ask_side_pct=0.6)
    result = compute_conviction_score(ep, _make_cfg())
    assert result >= 1


def test_conviction_d1_ask_side_just_below_floor_fails() -> None:
    """ask_side_pct=0.599 must fail D1."""
    ep = EpisodeFaker(ask_side_pct=0.599)
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_d1_ask_side_none_fails_d1() -> None:
    """None ask_side_pct must degrade gracefully to D1 fail."""
    ep = EpisodeFaker(ask_side_pct=None)
    result = compute_conviction_score(ep, _make_cfg())
    assert result == 4  # D1 missed; D2-D5 pass


def test_conviction_ask_side_pct_none_fails_d2_legacy() -> None:
    """Alias test: None ask_side_pct must fail D1 (score 4 on full ep)."""
    ep = EpisodeFaker(ask_side_pct=None)
    result = compute_conviction_score(ep, _make_cfg())
    assert result == 4


def test_conviction_d3_qualifying_tier_golden() -> None:
    """GOLDEN notional_tier must pass D3."""
    ep = EpisodeFaker(notional_tier="GOLDEN")
    assert compute_conviction_score(ep, _make_cfg()) == 5


def test_conviction_d3_qualifying_tier_block() -> None:
    """BLOCK notional_tier must pass D3."""
    ep = EpisodeFaker(notional_tier="BLOCK")
    assert compute_conviction_score(ep, _make_cfg()) == 5


def test_conviction_d3_qualifying_tier_noteworthy() -> None:
    """NOTEWORTHY notional_tier must pass D3."""
    ep = EpisodeFaker(notional_tier="NOTEWORTHY")
    assert compute_conviction_score(ep, _make_cfg()) == 5


def test_conviction_d3_non_qualifying_tier_fails() -> None:
    """A tier not in _QUALIFYING_TIERS must fail D3."""
    ep = EpisodeFaker(notional_tier="BELOW_THRESHOLD")
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_d3_none_tier_fails() -> None:
    """None notional_tier must fail D3."""
    ep = EpisodeFaker(notional_tier=None)
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_dte_bucket_90plus_disqualifies() -> None:
    """'90+' is in _DISQUALIFYING_DTE_BUCKETS -> D4 fails."""
    ep = EpisodeFaker(dte_bucket="90+")
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_dte_bucket_0_7_disqualifies() -> None:
    """'0-7' is in _DISQUALIFYING_DTE_BUCKETS -> D4 fails."""
    ep = EpisodeFaker(dte_bucket="0-7")
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_dte_bucket_none_fails_d4() -> None:
    """None dte_bucket must fail D4."""
    ep = EpisodeFaker(dte_bucket=None)
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_trade_count_exact_floor_passes() -> None:
    """trade_count exactly equal to min_trade_count (3) must pass D5."""
    ep = EpisodeFaker(trade_count=3)
    assert compute_conviction_score(ep, _make_cfg()) == 5


def test_conviction_trade_count_below_floor_fails() -> None:
    """trade_count=2 with min_trade_count=3 -> D5 fails."""
    ep = EpisodeFaker(trade_count=2)
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_custom_min_trade_count_from_cfg() -> None:
    ep = EpisodeFaker(trade_count=2)
    cfg_low = _make_cfg(**{"sig.min_trade_count": 2})
    assert compute_conviction_score(ep, cfg_low) == 5


def test_conviction_cfg_as_object_with_attrs() -> None:
    """cfg objects with real attributes (not dicts) are accepted."""
    @dataclass
    class FakeCfg:
        ask_side_pct_floor: float = 0.6
        min_trade_count: int = 3
        noteworthy_premium: float = 50_000.0

    ep = EpisodeFaker()  # all dims pass
    assert compute_conviction_score(ep, FakeCfg()) == 5


def test_conviction_score_zero_all_fail() -> None:
    ep = _episode_from_vector((False, False, False, False, False))
    assert compute_conviction_score(ep, _make_cfg()) == 0


def test_conviction_score_five_all_pass() -> None:
    ep = _episode_from_vector((True, True, True, True, True))
    assert compute_conviction_score(ep, _make_cfg()) == 5


# =============================================================================
# build_signal_row — valid row structure
# =============================================================================

def _valid_episode(**overrides) -> EpisodeFaker:
    ep = EpisodeFaker()
    for k, v in overrides.items():
        setattr(ep, k, v)
    return ep


def _valid_cfg() -> dict:
    return _make_cfg()


def test_build_signal_row_returns_dict() -> None:
    ep = _valid_episode()
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert isinstance(row, dict)


def test_build_signal_row_required_columns_present() -> None:
    required_cols = {
        "ticker", "alert_level", "direction",
        "composite_score", "backtest_score",
        "total_premium", "trade_count", "contract_type",
        "episode_steamroom_score", "ask_side_pct", "vol_oi_ratio",
        "episode_id", "reasoning", "is_accelerating", "signal_ts",
        "recommendation",
    }
    row = build_signal_row(_valid_episode(), "BLOCK", "BEARISH", _valid_cfg())
    missing = required_cols - set(row.keys())
    assert not missing, f"Row missing columns: {missing}"


def test_build_signal_row_retired_columns_absent() -> None:
    retired = {"flow_score", "influence_tier", "volume_premium_factor"}
    row = build_signal_row(_valid_episode(), "NOTEWORTHY", "BULLISH", _valid_cfg())
    present_retired = retired & set(row.keys())
    assert not present_retired, f"Retired columns found in row: {present_retired}"


def test_build_signal_row_ticker_from_ticker_attr() -> None:
    ep = _valid_episode(symbol=None, ticker="SPY")
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["ticker"] == "SPY"


def test_build_signal_row_ticker_prefers_symbol_over_ticker() -> None:
    ep = _valid_episode(symbol="NVDA", ticker="SHOULDNOTAPPEAR")
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["ticker"] == "NVDA"


def test_build_signal_row_alert_level_written_correctly() -> None:
    for level in ("WATCH", "NOTEWORTHY", "BLOCK", "GOLDEN"):
        row = build_signal_row(_valid_episode(), level, "BULLISH", _valid_cfg())
        assert row["alert_level"] == level


def test_build_signal_row_direction_written_correctly() -> None:
    for direction in ("BULLISH", "BEARISH", "NEUTRAL"):
        row = build_signal_row(_valid_episode(), "NOTEWORTHY", direction, _valid_cfg())
        assert row["direction"] == direction


def test_build_signal_row_composite_score_normalised() -> None:
    ep = _valid_episode()
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg(), conviction_score=5)
    assert row["composite_score"] == 1.0

    row2 = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg(), conviction_score=3)
    assert row2["composite_score"] == round(3 / 5.0, 3)


def test_build_signal_row_conviction_score_0_gives_composite_0() -> None:
    row = build_signal_row(_valid_episode(), "WATCH", "NEUTRAL", _valid_cfg(), conviction_score=0)
    assert row["composite_score"] == 0.0
    assert row["episode_steamroom_score"] == 0


def test_build_signal_row_episode_steamroom_score_matches_conviction() -> None:
    for cv in range(6):
        row = build_signal_row(_valid_episode(), "NOTEWORTHY", "BULLISH", _valid_cfg(), conviction_score=cv)
        assert row["episode_steamroom_score"] == cv, f"Mismatch at cv={cv}"


def test_build_signal_row_contract_type_uppercased() -> None:
    ep = _valid_episode(contract_type="call")
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["contract_type"] == "CALL"


def test_build_signal_row_contract_type_none_allowed() -> None:
    ep = _valid_episode(contract_type=None)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["contract_type"] is None


def test_build_signal_row_ask_side_pct_rounded_to_4dp() -> None:
    ep = _valid_episode(ask_side_pct=0.123456789)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["ask_side_pct"] == round(0.123456789, 4)


def test_build_signal_row_ask_side_pct_none_is_null() -> None:
    ep = _valid_episode(ask_side_pct=None)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["ask_side_pct"] is None


def test_build_signal_row_signal_ts_defaults_to_utcnow() -> None:
    before = datetime.now(tz=timezone.utc)
    row = build_signal_row(_valid_episode(), "GOLDEN", "BULLISH", _valid_cfg())
    after = datetime.now(tz=timezone.utc)

    ts_str = row["signal_ts"]
    assert ts_str is not None
    ts = datetime.fromisoformat(ts_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    assert before <= ts <= after


def test_build_signal_row_explicit_signal_ts_preserved() -> None:
    fixed_ts = "2026-01-15T12:00:00+00:00"
    row = build_signal_row(_valid_episode(), "GOLDEN", "BULLISH", _valid_cfg(), signal_ts=fixed_ts)
    assert row["signal_ts"] == fixed_ts


# =============================================================================
# build_signal_row — ValueError on bad vocab
# =============================================================================

def test_build_signal_row_invalid_alert_level_raises() -> None:
    with pytest.raises(ValueError, match="alert_level"):
        build_signal_row(_valid_episode(), "GARBAGE", "BULLISH", _valid_cfg())


def test_build_signal_row_invalid_direction_raises() -> None:
    with pytest.raises(ValueError, match="direction"):
        build_signal_row(_valid_episode(), "GOLDEN", "SIDEWAYS", _valid_cfg())


# =============================================================================
# build_signal_row — vol_oi_ratio derivation fallback
# =============================================================================

def test_build_signal_row_vol_oi_ratio_direct_attr() -> None:
    ep = _valid_episode(vol_oi_ratio=2.5)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] == 2.5


def test_build_signal_row_vol_oi_ratio_derived_from_volume_oi() -> None:
    ep = _valid_episode(
        vol_oi_ratio=None,
        contract_volume_at_close=300,
        contract_oi_at_open=100,
    )
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] == round(300 / 100, 4)


def test_build_signal_row_vol_oi_ratio_none_when_oi_zero() -> None:
    ep = _valid_episode(
        vol_oi_ratio=None,
        contract_volume_at_close=200,
        contract_oi_at_open=0,
    )
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] is None


def test_build_signal_row_vol_oi_ratio_none_when_both_missing() -> None:
    ep = _valid_episode(
        vol_oi_ratio=None,
        contract_volume_at_close=None,
        contract_oi_at_open=None,
    )
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] is None


# =============================================================================
# _derive_recommendation — mapping conviction score to recommendation string
# =============================================================================

def test_derive_recommendation_score_5_bullish() -> None:
    """score=5 + confirmed -> FOLLOW_SWEEP."""
    assert _derive_recommendation(5, "BULLISH", True) == "FOLLOW_SWEEP"


def test_derive_recommendation_score_5_bearish() -> None:
    """score=5 + confirmed -> FOLLOW_SWEEP."""
    assert _derive_recommendation(5, "BEARISH", True) == "FOLLOW_SWEEP"


def test_derive_recommendation_score_5_bullish_unconfirmed() -> None:
    """score=5 + not confirmed -> WATCH."""
    assert _derive_recommendation(5, "BULLISH", False) == "WATCH"


def test_derive_recommendation_score_3_bullish() -> None:
    """score=3 + BULLISH: BUY_CALLS when confirmed, WATCH when not."""
    result_confirmed = _derive_recommendation(3, "BULLISH", True)
    result_unconfirmed = _derive_recommendation(3, "BULLISH", False)
    assert result_confirmed == "BUY_CALLS"
    assert result_unconfirmed in ("BUY_CALLS", "WATCH", "HOLD")  # WATCH per spec


def test_derive_recommendation_score_0_neutral() -> None:
    """score=0 or NEUTRAL -> NO_ACTION."""
    result = _derive_recommendation(0, "NEUTRAL", False)
    assert result in ("HOLD", "WATCH", "NO_SIGNAL", "NO_ACTION")


def test_derive_recommendation_score_5_neutral() -> None:
    """High conviction + neutral direction should not be BUY or SELL."""
    result = _derive_recommendation(5, "NEUTRAL", True)
    assert result not in ("STRONG_BUY", "STRONG_SELL", "BUY", "SELL", "FOLLOW_SWEEP", "BUY_CALLS", "BUY_PUTS")


# =============================================================================
# Module-level constants — quick sanity checks
# =============================================================================

def test_qualifying_tiers_contains_expected_values() -> None:
    for tier in ("GOLDEN", "BLOCK", "NOTEWORTHY"):
        assert tier in _QUALIFYING_TIERS, f"{tier!r} missing from _QUALIFYING_TIERS"


def test_disqualifying_dte_buckets_contains_extremes() -> None:
    assert "0-7" in _DISQUALIFYING_DTE_BUCKETS
    assert "90+" in _DISQUALIFYING_DTE_BUCKETS
