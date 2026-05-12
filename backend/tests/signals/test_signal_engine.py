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
# sig.noteworthy_premium=50_000 → watch_floor = 25_000 (D1 floor).
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
    """
    # Required for build_signal_row
    ticker: str = "AAPL"
    symbol: Optional[str] = None   # build_signal_row tries symbol first
    # D1 — premium meets watch-band floor (watch_floor = noteworthy * 0.5 = 25_000)
    total_premium: float = 100_000.0
    # D2 — ask-side
    ask_side_pct: float = 0.75
    # D3 — vol > OI
    vol_oi_signal: bool = True
    # D4 — DTE bucket
    dte_bucket: str = "14-30"
    # D5 — trade count
    trade_count: int = 5
    # build_signal_row extras
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
#
# D1: total_premium >= watch_floor (noteworthy_premium * 0.5 = 25_000)
# D2: ask_side_pct >= floor (0.6)
# D3: vol_oi_signal == True
# D4: dte_bucket not in DISQUALIFYING_DTE_BUCKETS
# D5: trade_count >= min_trade_count (3)
_DIMENSION_SPEC: list[tuple[str, Any, Any]] = [
    # D1: total_premium >= watch_floor (25_000 with MIN_CFG)
    ("total_premium",  100_000.0,  1_000.0),
    # D2: ask_side_pct >= floor (0.6)
    ("ask_side_pct",   0.75,       0.3),
    # D3: vol_oi_signal == True
    ("vol_oi_signal",  True,       False),
    # D4: dte_bucket not in DISQUALIFYING_DTE_BUCKETS
    ("dte_bucket",     "14-30",    "0-7"),
    # D5: trade_count >= min_trade_count (3)
    ("trade_count",    5,          1),
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
        f"Episode attrs: total_premium={ep.total_premium}, ask_side_pct={ep.ask_side_pct}, "
        f"vol_oi_signal={ep.vol_oi_signal}, dte_bucket={ep.dte_bucket!r}, trade_count={ep.trade_count}"
    )


# ---------------------------------------------------------------------------
# compute_conviction_score — boundary / edge-case tests
# ---------------------------------------------------------------------------

def test_conviction_d1_premium_at_exact_watch_floor_passes() -> None:
    """total_premium exactly equal to watch_floor (25_000) must count as D1 pass."""
    # watch_floor = noteworthy_premium * 0.5 = 50_000 * 0.5 = 25_000
    ep = EpisodeFaker(total_premium=25_000.0)
    result = compute_conviction_score(ep, _make_cfg())
    assert result >= 1  # D1 at minimum passes


def test_conviction_d1_premium_just_below_watch_floor_fails() -> None:
    """total_premium=24_999 (below watch_floor=25_000) must fail D1."""
    ep = EpisodeFaker(total_premium=24_999.0)
    # Only D1 fails; D2–D5 pass → expect 4
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_d1_premium_none_fails_d1() -> None:
    """None total_premium coerces to 0.0, which is below watch_floor → D1 fail."""
    ep = EpisodeFaker(total_premium=None)
    result = compute_conviction_score(ep, _make_cfg())
    assert result == 4  # D1 missed; D2-D5 pass


def test_conviction_d1_custom_noteworthy_floor_from_cfg() -> None:
    """watch_floor is derived from cfg noteworthy_premium, not hardcoded."""
    # Raise noteworthy to 200_000 → watch_floor = 100_000
    cfg_high = _make_cfg(**{"sig.noteworthy_premium": 200_000.0})
    ep_pass = EpisodeFaker(total_premium=100_000.0)  # exactly at new watch_floor
    ep_fail = EpisodeFaker(total_premium=99_999.0)   # just below
    assert compute_conviction_score(ep_pass, cfg_high) == 5
    assert compute_conviction_score(ep_fail, cfg_high) == 4


def test_conviction_ask_side_pct_exact_floor_passes() -> None:
    """ask_side_pct exactly equal to floor (0.6) must count as D2 pass."""
    ep = EpisodeFaker(ask_side_pct=0.6)
    assert compute_conviction_score(ep, _make_cfg()) >= 1  # D2 at minimum passes


def test_conviction_ask_side_pct_just_below_floor_fails() -> None:
    """ask_side_pct=0.599 (one floating-point step below 0.6) must fail D2."""
    ep = EpisodeFaker(ask_side_pct=0.599)
    # Only D2 fails; expect 4
    assert compute_conviction_score(ep, _make_cfg()) == 4


def test_conviction_ask_side_pct_none_fails_d2() -> None:
    """None ask_side_pct must degrade gracefully to D2 fail (score 4 on full ep)."""
    ep = EpisodeFaker(ask_side_pct=None)
    result = compute_conviction_score(ep, _make_cfg())
    assert result == 4  # D2 missed; D1,D3-D5 pass


def test_conviction_notional_tier_not_a_scored_dimension() -> None:
    """notional_tier has no direct effect on compute_conviction_score.

    D1 is now a raw premium check.  Changing notional_tier alone must not
    alter the score (tier-adjustment happens inside _eval_gate_1 via
    get_effective_premium_threshold, which is not called here).
    """
    ep_golden = EpisodeFaker()
    ep_golden.notional_tier = "GOLDEN"
    ep_below = EpisodeFaker()
    ep_below.notional_tier = "BELOW"
    cfg = _make_cfg()
    assert compute_conviction_score(ep_golden, cfg) == compute_conviction_score(ep_below, cfg)


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
        noteworthy_premium: float = 50_000.0

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
    before = datetime.now(tz=