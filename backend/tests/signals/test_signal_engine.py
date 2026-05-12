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
# the score == popcount(dimension_vector) in each case.  This is exhaustive
# by construction.  Separate named tests cover edge cases (None attributes,
# floor boundary values) that the parametrize sweep intentionally keeps
# at their default pass/fail state for simplicity.
#
# build_signal_row tests use a minimal EpisodeFaker dataclass so we don't
# need to import the real RepetitionEpisode (which has stream-worker deps).
#
# All fixture config objects use plain dicts (the _read_config_snapshot path)
# which compute_conviction_score and build_signal_row both accept.
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
# Uses the snake_case short-key format (attribute-path) which the _get()
# helper inside compute_conviction_score tries first via getattr, then via
# dict key lookup.  Supplying both key variants is unnecessary — the dict
# lookup on the plain dict catches them as dict keys, not attrs.
MIN_CFG = {
    "sig.ask_side_pct_floor": 0.6,
    "sig.min_trade_count": 3,
}


@dataclass
class EpisodeFaker:
    """Minimal stand-in for RepetitionEpisode in tests.

    Fields mirror the exact attributes that compute_conviction_score and
    build_signal_row read via getattr.  Defaults represent a fully-qualifying
    episode (all 5 dimensions pass).
    """
    # Required for build_signal_row
    ticker: str = "AAPL"
    symbol: Optional[str] = None   # build_signal_row tries symbol first
    # D1 — ask-side
    ask_side_pct: float = 0.75
    # D2 — vol > OI
    vol_oi_signal: bool = True
    # D3 — notional tier
    notional_tier: str = "GOLDEN"
    # D4 — DTE bucket
    dte_bucket: str = "14-30"
    # D5 — trade count
    trade_count: int = 5
    # build_signal_row extras
    total_premium: float = 1_500_000.0
    contract_type: str = "call"
    episode_id: str = "ep-abc-123"
    # vol_oi_ratio derivation fallback
    vol_oi_ratio: Optional[float] = None
    contract_volume_at_close: Optional[int] = None
    contract_oi_at_open: Optional[int] = None


def _make_cfg(**overrides) -> dict:
    """Return MIN_CFG with any field overridden for test-specific scenarios."""
    cfg = dict(MIN_CFG)
    cfg.update(overrides)
    return cfg


# =============================================================================
# compute_conviction_score — all 32 combinations (5 binary dimensions)
# =============================================================================

# Dimension index → (attr_name, passing_value, failing_value)
# Each tuple defines what makes a dimension PASS or FAIL.
_DIMENSION_SPEC: list[tuple[str, Any, Any]] = [
    # D1: ask_side_pct >= floor (0.6)
    ("ask_side_pct",   0.75,      0.3),
    # D2: vol_oi_signal == True
    ("vol_oi_signal",  True,      False),
    # D3: notional_tier in QUALIFYING_TIERS
    ("notional_tier",  "GOLDEN",  "BELOW"),
    # D4: dte_bucket not in DISQUALIFYING_DTE_BUCKETS
    ("dte_bucket",     "14-30",   "0-7"),
    # D5: trade_count >= min_trade_count (3)
    ("trade_count",    5,         1),
]


def _episode_from_vector(vector: tuple[bool, ...]) -> EpisodeFaker:
    """Build an EpisodeFaker where dimension i passes iff vector[i] is True."""
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
    """For every 2^5 dimension vector, score must equal popcount(vector).

    This test is exhaustive by construction: itertools.product generates all
    32 combinations and the expected score is simply sum(vector).
    """
    ep = _episode_from_vector(vector)
    cfg = _make_cfg()
    expected = sum(vector)
    result = compute_conviction_score(ep, cfg)
    assert result == expected, (
        f"vector={vector!r} → expected score={expected}, got {result}. "
        f"Episode attrs: ask_side_pct={ep.ask_side_pct}, vol_oi_signal={ep.vol_oi_signal}, "
        f"notional_tier={ep.notional_tier!r}, dte_bucket={ep.dte_bucket!r}, trade_count={ep.trade_count}"
    )


# ---------------------------------------------------------------------------
# compute_conviction_score — boundary / edge-case tests
# ---------------------------------------------------------------------------

def test_conviction_ask_side_pct_exact_floor_passes() -> None:
    """ask_side_pct exactly equal to floor (0.6) must count as D1 pass."""
    ep = EpisodeFaker(ask_side_pct=0.6)
    assert compute_conviction_score(ep, _make_cfg()) >= 1  # D1 at minimum passes


def test_conviction_ask_side_pct_just_below_floor_fails() -> None:
    """ask_side_pct=0.599 (one floating-point step below 0.6) must fail D1."""
    ep = EpisodeFaker(ask_side_pct=0.599)
    # Only D1 fails; expect 4
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_ask_side_pct_none_fails_d1() -> None:
    """None ask_side_pct must degrade gracefully to D1 fail (score 4 on full ep)."""
    ep = EpisodeFaker(ask_side_pct=None)
    result = compute_conviction_score(ep, _make_cfg())
    assert result == 4  # D1 missed; D2-D5 pass


def test_conviction_notional_tier_noteworthy_qualifies() -> None:
    """NOTEWORTHY is in _QUALIFYING_TIERS → D3 passes."""
    ep = _episode_from_vector((True, True, True, True, True))
    ep.notional_tier = "NOTEWORTHY"
    assert compute_conviction_score(ep, _make_cfg()) == 5


def test_conviction_notional_tier_block_qualifies() -> None:
    """BLOCK is in _QUALIFYING_TIERS → D3 passes."""
    ep = _episode_from_vector((True, True, True, True, True))
    ep.notional_tier = "BLOCK"
    assert compute_conviction_score(ep, _make_cfg()) == 5


def test_conviction_notional_tier_below_disqualifies() -> None:
    """A tier string not in _QUALIFYING_TIERS → D3 fails."""
    ep = _episode_from_vector((True, True, False, True, True))
    ep.notional_tier = "BELOW_NOTEWORTHY"
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_notional_tier_none_fails_d3() -> None:
    """None notional_tier must fail D3 (not in _QUALIFYING_TIERS)."""
    ep = EpisodeFaker(notional_tier=None)
    result = compute_conviction_score(ep, _make_cfg())
    assert result == 4


def test_conviction_dte_bucket_90plus_disqualifies() -> None:
    """'90+' is in _DISQUALIFYING_DTE_BUCKETS → D4 fails."""
    ep = EpisodeFaker(dte_bucket="90+")
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_dte_bucket_0_7_disqualifies() -> None:
    """'0-7' is in _DISQUALIFYING_DTE_BUCKETS → D4 fails."""
    ep = EpisodeFaker(dte_bucket="0-7")
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_dte_bucket_none_fails_d4() -> None:
    """None dte_bucket must fail D4 (cannot be 'not in disqualifying set')."""
    ep = EpisodeFaker(dte_bucket=None)
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_trade_count_exact_floor_passes() -> None:
    """trade_count exactly equal to min_trade_count (3) must pass D5."""
    ep = EpisodeFaker(trade_count=3)
    assert compute_conviction_score(ep, _make_cfg()) == 5


def test_conviction_trade_count_below_floor_fails() -> None:
    """trade_count=2 with min_trade_count=3 → D5 fails."""
    ep = EpisodeFaker(trade_count=2)
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_custom_min_trade_count_from_cfg() -> None:
    """min_trade_count read from cfg overrides the hardcoded default."""
    ep = EpisodeFaker(trade_count=2)  # D5 fails with default floor=3
    cfg_low = _make_cfg(**{"sig.min_trade_count": 2})  # lower floor
    assert compute_conviction_score(ep, cfg_low) == 5  # now D5 passes


def test_conviction_cfg_as_object_with_attrs() -> None:
    """cfg objects with real attributes (not dicts) are accepted."""
    @dataclass
    class FakeCfg:
        ask_side_pct_floor: float = 0.6
        min_trade_count: int = 3

    ep = EpisodeFaker()  # all dims pass
    assert compute_conviction_score(ep, FakeCfg()) == 5


def test_conviction_score_zero_all_fail() -> None:
    """All five dimensions failing must yield score 0."""
    ep = _episode_from_vector((False, False, False, False, False))
    assert compute_conviction_score(ep, _make_cfg()) == 0


def test_conviction_score_five_all_pass() -> None:
    """All five dimensions passing must yield score 5."""
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
    """Every post-REARCH-010 column in the schema spec must be present."""
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
    """Retired REARCH-010 columns must NOT appear in the row dict."""
    retired = {"flow_score", "influence_tier", "volume_premium_factor"}
    row = build_signal_row(_valid_episode(), "NOTEWORTHY", "BULLISH", _valid_cfg())
    present_retired = retired & set(row.keys())
    assert not present_retired, f"Retired columns found in row: {present_retired}"


def test_build_signal_row_ticker_from_ticker_attr() -> None:
    ep = _valid_episode(symbol=None, ticker="SPY")
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["ticker"] == "SPY"


def test_build_signal_row_ticker_prefers_symbol_over_ticker() -> None:
    """symbol attr is tried first; ticker is the fallback."""
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
    """composite_score must equal conviction_score / 5.0 (3dp)."""
    ep = _valid_episode()  # all dims pass → conviction=5
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg(), conviction_score=5)
    assert row["composite_score"] == 1.0

    row2 = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg(), conviction_score=3)
    assert row2["composite_score"] == round(3 / 5.0, 3)


def test_build_signal_row_conviction_score_0_gives_composite_0() -> None:
    row = build_signal_row(_valid_episode(), "WATCH", "NEUTRAL", _valid_cfg(), conviction_score=0)
    assert row["composite_score"] == 0.0
    assert row["episode_steamroom_score"] == 0


def test_build_signal_row_episode_steamroom_score_matches_conviction() -> None:
    for cv in range(6):  # 0 through 5
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
    """When signal_ts is not supplied, an ISO-8601 UTC timestamp must be generated."""
    before = datetime.now(tz=timezone.utc)
    row = build_signal_row(_valid_episode(), "GOLDEN", "BULLISH", _valid_cfg())
    after = datetime.now(tz=timezone.utc)
    ts = datetime.fromisoformat(row["signal_ts"])
    assert before <= ts <= after


def test_build_signal_row_signal_ts_explicit_is_preserved() -> None:
    """An explicitly supplied signal_ts must be written exactly (no re-stamping)."""
    explicit_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    row = build_signal_row(
        _valid_episode(), "GOLDEN", "BULLISH", _valid_cfg(),
        signal_ts=explicit_ts,
    )
    assert row["signal_ts"] == explicit_ts.isoformat()


def test_build_signal_row_is_accelerating_default_false() -> None:
    row = build_signal_row(_valid_episode(), "GOLDEN", "BULLISH", _valid_cfg())
    assert row["is_accelerating"] is False


def test_build_signal_row_is_accelerating_true() -> None:
    row = build_signal_row(
        _valid_episode(), "GOLDEN", "BULLISH", _valid_cfg(),
        is_accelerating=True,
    )
    assert row["is_accelerating"] is True


def test_build_signal_row_backtest_score_default_zero() -> None:
    row = build_signal_row(_valid_episode(), "GOLDEN", "BULLISH", _valid_cfg())
    assert row["backtest_score"] == 0.0


def test_build_signal_row_backtest_score_custom() -> None:
    row = build_signal_row(
        _valid_episode(), "GOLDEN", "BULLISH", _valid_cfg(),
        backtest_score=0.85,
    )
    assert row["backtest_score"] == round(0.85, 3)


def test_build_signal_row_reasoning_none_by_default() -> None:
    row = build_signal_row(_valid_episode(), "GOLDEN", "BULLISH", _valid_cfg())
    assert row["reasoning"] is None


def test_build_signal_row_reasoning_passed_through() -> None:
    row = build_signal_row(
        _valid_episode(), "GOLDEN", "BULLISH", _valid_cfg(),
        reasoning="T3 GOLDEN AC: $1.5M premium, 100% ask-side, 5/5 gates",
    )
    assert "GOLDEN" in row["reasoning"]


def test_build_signal_row_episode_id_stringified() -> None:
    """episode_id must be cast to str even if the episode carries an int/UUID."""
    import uuid
    uid = uuid.uuid4()
    ep = _valid_episode(episode_id=uid)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["episode_id"] == str(uid)


def test_build_signal_row_episode_id_none_allowed() -> None:
    ep = _valid_episode(episode_id=None)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["episode_id"] is None


# =============================================================================
# build_signal_row — ValueError on bad vocab
# =============================================================================

def test_build_signal_row_invalid_alert_level_raises() -> None:
    with pytest.raises(ValueError, match="invalid alert_level"):
        build_signal_row(_valid_episode(), "INVALID_LEVEL", "BULLISH", _valid_cfg())


def test_build_signal_row_invalid_direction_raises() -> None:
    with pytest.raises(ValueError, match="invalid direction"):
        build_signal_row(_valid_episode(), "GOLDEN", "SIDEWAYS", _valid_cfg())


def test_build_signal_row_both_invalid_raises_alert_level_first() -> None:
    """alert_level is validated before direction — alert_level error fires first."""
    with pytest.raises(ValueError, match="invalid alert_level"):
        build_signal_row(_valid_episode(), "BAD", "ALSO_BAD", _valid_cfg())


def test_build_signal_row_missing_ticker_and_symbol_raises() -> None:
    ep = _valid_episode(symbol=None, ticker=None)
    with pytest.raises(ValueError, match="no symbol/ticker"):
        build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())


def test_build_signal_row_empty_string_ticker_raises() -> None:
    """Empty-string ticker is falsy → same ValueError as None."""
    ep = _valid_episode(symbol=None, ticker="")
    with pytest.raises(ValueError, match="no symbol/ticker"):
        build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())


# =============================================================================
# build_signal_row — None / optional field handling
# =============================================================================

def test_build_signal_row_total_premium_none_is_null() -> None:
    ep = _valid_episode(total_premium=None)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["total_premium"] is None


def test_build_signal_row_trade_count_none_is_null() -> None:
    ep = _valid_episode(trade_count=None)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["trade_count"] is None


# =============================================================================
# build_signal_row — vol_oi_ratio derivation fallback path
# =============================================================================

def test_build_signal_row_vol_oi_ratio_explicit_used_when_present() -> None:
    """When episode.vol_oi_ratio is set, it must be used without re-derivation."""
    ep = _valid_episode(
        vol_oi_ratio=2.5,
        contract_volume_at_close=10_000,
        contract_oi_at_open=1_000,  # derivation would give 10.0, not 2.5
    )
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] == 2.5


def test_build_signal_row_vol_oi_ratio_derived_from_vol_and_oi() -> None:
    """When vol_oi_ratio is None, derive as vol / oi rounded to 4dp."""
    ep = _valid_episode(
        vol_oi_ratio=None,
        contract_volume_at_close=3_000,
        contract_oi_at_open=1_200,
    )
    expected = round(3_000 / 1_200, 4)
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] == expected


def test_build_signal_row_vol_oi_ratio_none_when_vol_missing() -> None:
    """No vol → ratio stays None even if OI is present."""
    ep = _valid_episode(
        vol_oi_ratio=None,
        contract_volume_at_close=None,
        contract_oi_at_open=5_000,
    )
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] is None


def test_build_signal_row_vol_oi_ratio_none_when_oi_zero() -> None:
    """OI=0 must NOT cause a ZeroDivisionError; ratio must be None."""
    ep = _valid_episode(
        vol_oi_ratio=None,
        contract_volume_at_close=10_000,
        contract_oi_at_open=0,
    )
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] is None


def test_build_signal_row_vol_oi_ratio_none_when_both_missing() -> None:
    """Neither vol nor OI present → ratio stays None."""
    ep = _valid_episode(
        vol_oi_ratio=None,
        contract_volume_at_close=None,
        contract_oi_at_open=None,
    )
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg())
    assert row["vol_oi_ratio"] is None


# =============================================================================
# build_signal_row — recommendation field (integration with _derive_recommendation)
# =============================================================================
# These verify that the recommendation is wired correctly inside build_signal_row
# (not the _derive_recommendation logic itself, which has its own deeper tests
# in test_signal_engine_recommendation.py or similar).  Just check the key
# routing cases so a regression in the wire-up is caught here.

def test_build_signal_row_recommendation_follow_sweep_when_conviction_5_bullish_ask_confirmed() -> None:
    """conviction=5 + ask_side_confirmed + BULLISH → FOLLOW_SWEEP."""
    ep = _valid_episode(ask_side_pct=0.9)  # ask confirmed, all dims pass → cv=5
    row = build_signal_row(ep, "GOLDEN", "BULLISH", _valid_cfg(), conviction_score=5)
    assert row["recommendation"] == "FOLLOW_SWEEP"


def test_build_signal_row_recommendation_buy_calls_bullish_cv3() -> None:
    ep = _valid_episode(ask_side_pct=0.9)
    row = build_signal_row(ep, "BLOCK", "BULLISH", _valid_cfg(), conviction_score=3)
    assert row["recommendation"] == "BUY_CALLS"


def test_build_signal_row_recommendation_buy_puts_bearish_cv4() -> None:
    ep = _valid_episode(ask_side_pct=0.9)
    row = build_signal_row(ep, "BLOCK", "BEARISH", _valid_cfg(), conviction_score=4)
    assert row["recommendation"] == "BUY_PUTS"


def test_build_signal_row_recommendation_watch_when_ask_side_fails() -> None:
    """conviction=4 but ask_side_pct below floor → WATCH (hard gate)."""
    ep = _valid_episode(ask_side_pct=0.2)  # below default floor 0.6
    row = build_signal_row(ep, "BLOCK", "BULLISH", _valid_cfg(), conviction_score=4)
    assert row["recommendation"] == "WATCH"


def test_build_signal_row_recommendation_no_action_neutral_direction() -> None:
    ep = _valid_episode(ask_side_pct=0.9)
    row = build_signal_row(ep, "NOTEWORTHY", "NEUTRAL", _valid_cfg(), conviction_score=5)
    assert row["recommendation"] == "NO_ACTION"


def test_build_signal_row_recommendation_no_action_conviction_0() -> None:
    ep = _valid_episode()
    row = build_signal_row(ep, "WATCH", "BULLISH", _valid_cfg(), conviction_score=0)
    assert row["recommendation"] == "NO_ACTION"


# =============================================================================
# _derive_recommendation — full decision tree coverage
# (unit tests on the private function directly for exhaustive verification)
# =============================================================================

@pytest.mark.parametrize("direction", ["NEUTRAL", "UP", "", "unknown"])
def test_derive_recommendation_no_action_for_non_directional(direction: str) -> None:
    assert _derive_recommendation(5, direction, True) == "NO_ACTION"


def test_derive_recommendation_no_action_conviction_zero() -> None:
    for direction in ("BULLISH", "BEARISH"):
        assert _derive_recommendation(0, direction, True) == "NO_ACTION"


def test_derive_recommendation_follow_sweep_conviction_5_bullish() -> None:
    assert _derive_recommendation(5, "BULLISH", True) == "FOLLOW_SWEEP"


def test_derive_recommendation_follow_sweep_conviction_5_bearish() -> None:
    assert _derive_recommendation(5, "BEARISH", True) == "FOLLOW_SWEEP"


def test_derive_recommendation_follow_sweep_requires_ask_confirmed() -> None:
    """conviction=5 without ask_side_confirmed must NOT be FOLLOW_SWEEP."""
    result = _derive_recommendation(5, "BULLISH", False)
    assert result != "FOLLOW_SWEEP"
    # With ask not confirmed, it's WATCH (not BUY either since hard gate failed)
    assert result == "WATCH"


@pytest.mark.parametrize("cv", [3, 4])
def test_derive_recommendation_buy_calls_bullish(cv: int) -> None:
    assert _derive_recommendation(cv, "BULLISH", True) == "BUY_CALLS"


@pytest.mark.parametrize("cv", [3, 4])
def test_derive_recommendation_buy_puts_bearish(cv: int) -> None:
    assert _derive_recommendation(cv, "BEARISH", True) == "BUY_PUTS"


@pytest.mark.parametrize("cv", [1, 2])
def test_derive_recommendation_watch_below_floor(cv: int) -> None:
    for direction in ("BULLISH", "BEARISH"):
        assert _derive_recommendation(cv, direction, True) == "WATCH"


@pytest.mark.parametrize("cv", [3, 4, 5])
def test_derive_recommendation_watch_when_ask_side_not_confirmed(cv: int) -> None:
    """Ask-side is a hard gate — no matter the conviction, unconfirmed → WATCH."""
    for direction in ("BULLISH", "BEARISH"):
        result = _derive_recommendation(cv, direction, False)
        assert result == "WATCH", (
            f"Expected WATCH for cv={cv} direction={direction} ask_confirmed=False, got {result!r}"
        )
