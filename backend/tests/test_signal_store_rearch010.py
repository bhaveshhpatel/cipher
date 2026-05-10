"""
Rearch-010 pre-merge unit tests — Item 5:
  signal_store._build_row() must NOT emit any of the retired keys.

  Retired in migration 024 / REARCH-010:
    - flow_score              (dropped; composite_score is sole score surface)
    - backtest_score          (column was never in signal_history — pre-rearch artifact)
    - volume_premium_factor   (column dropped in migration 024)
    - swarm_direction         (swarm fields dropped in migration 024)
    - swarm_confidence
    - swarm_agents
    - swarm_bull_votes
    - swarm_bear_votes
    - swarm_hold_votes
    - influence_tier          (column dropped in migration 024)
    - is_golden_sweep         (column dropped in migration 024)

  Also validates REARCH vocab on _VALID_ALERT_LEVELS and _normalise_alert_level().

These tests import _build_row directly — they do not require Supabase
or any network calls. They run cleanly in CI with no env vars set.
"""
import pytest

from services.signal_store import (
    _build_row,
    _normalise_alert_level,
    _normalise_direction,
    _VALID_ALERT_LEVELS,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_SIG = {
    "ticker":          "SPY",
    "recommendation":  "BUY",
    "composite_score": 0.72,
    "reasoning":       "test",
    "alert_level":     "NOTEWORTHY",
    "direction":       "BULLISH",
    "sentiment":       "BULLISH",
    "total_premium":   150_000,
    "trade_type":      "sweep",
    "contract_type":   "CALL",
}

_MINIMAL_EP = {
    "total_premium":  150_000,
    "trade_count":    3,
    "is_accelerating": True,
    "direction":      "BULLISH",
    "trade_type":     "sweep",
    "contract_type":  "CALL",
    "timestamp":      "2026-05-09T09:45:00Z",
}

_SWARM_POLLUTED_SIG = {
    **_MINIMAL_SIG,
    # These would have been written pre-rearch-010:
    "swarm_direction":  "BULLISH",
    "swarm_confidence": 0.85,
    "swarm_agents":     5,
    "swarm_bull_votes": 4,
    "swarm_bear_votes": 0,
    "swarm_hold_votes": 1,
    "influence_tier":   "WHALE",
    "is_golden_sweep":  True,
    "flow_score":       0.65,
    "backtest_score":   0.71,
    "volume_premium_factor": 1.3,
}


# ---------------------------------------------------------------------------
# Retired column tests — core
# ---------------------------------------------------------------------------

class TestRetiredColumnsAbsent:
    """_build_row() must never include retired column names in its output dict."""

    RETIRED_KEYS = [
        "flow_score",
        "backtest_score",
        "volume_premium_factor",
        "swarm_direction",
        "swarm_confidence",
        "swarm_agents",
        "swarm_bull_votes",
        "swarm_bear_votes",
        "swarm_hold_votes",
        "influence_tier",
        "is_golden_sweep",
    ]

    @pytest.mark.parametrize("key", RETIRED_KEYS)
    def test_retired_key_absent_minimal_sig(self, key):
        """Retired key must not appear when signal has no legacy fields."""
        row = _build_row(_MINIMAL_SIG, _MINIMAL_EP)
        assert key not in row, (
            f"'{key}' found in _build_row() output — "
            "it was retired in migration 024 and must not be written."
        )

    @pytest.mark.parametrize("key", RETIRED_KEYS)
    def test_retired_key_absent_even_if_present_in_sig(self, key):
        """
        Even if upstream passes a signal dict that still contains retired keys
        (e.g. un-redeployed caller), _build_row() must silently drop them.
        """
        row = _build_row(_SWARM_POLLUTED_SIG, _MINIMAL_EP)
        assert key not in row, (
            f"'{key}' leaked into _build_row() output from upstream signal payload. "
            "_build_row() uses explicit key access (sig.get()) so unknown keys "
            "must never appear in the returned dict."
        )


# ---------------------------------------------------------------------------
# composite_score is the sole score surface
# ---------------------------------------------------------------------------

class TestCompositScoreIsSoleSurface:
    def test_composite_score_present(self):
        row = _build_row(_MINIMAL_SIG, _MINIMAL_EP)
        assert "composite_score" in row
        assert row["composite_score"] == 0.72

    def test_composite_score_not_shadowed_by_flow_score(self):
        sig = {**_MINIMAL_SIG, "flow_score": 0.99}
        row = _build_row(sig, _MINIMAL_EP)
        assert row.get("composite_score") == 0.72, (
            "composite_score was overwritten or lost"
        )
        assert "flow_score" not in row


# ---------------------------------------------------------------------------
# REARCH vocab enforcement on alert_level
# ---------------------------------------------------------------------------

class TestAlertLevelReArchVocab:

    def test_valid_alert_levels_set_is_rearch_only(self):
        assert _VALID_ALERT_LEVELS == {"WATCH", "NOTEWORTHY", "BLOCK", "GOLDEN"}, (
            f"_VALID_ALERT_LEVELS has unexpected values: {_VALID_ALERT_LEVELS}. "
            "Pre-REARCH values (CONVICTION/WHALE/INSTITUTIONAL/LARGE/RETAIL) "
            "must not be in the set — they are in the legacy bridge only."
        )

    @pytest.mark.parametrize("level", ["WATCH", "NOTEWORTHY", "BLOCK", "GOLDEN"])
    def test_valid_rearch_levels_pass_through(self, level):
        assert _normalise_alert_level(level) == level

    @pytest.mark.parametrize("legacy,expected", [
        ("CONVICTION",    "BLOCK"),
        ("WHALE",         "BLOCK"),
        ("INSTITUTIONAL", "NOTEWORTHY"),
        ("LARGE",         "NOTEWORTHY"),
        ("RETAIL",        "WATCH"),
    ])
    def test_legacy_levels_bridge_to_rearch(self, legacy, expected):
        result = _normalise_alert_level(legacy)
        assert result == expected, (
            f"Legacy alert_level {legacy!r} should bridge to {expected!r}, got {result!r}"
        )

    @pytest.mark.parametrize("invalid", ["CONVICTION", "WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"])
    def test_pre_rearch_levels_not_in_valid_set(self, invalid):
        assert invalid not in _VALID_ALERT_LEVELS, (
            f"Pre-REARCH value {invalid!r} is in _VALID_ALERT_LEVELS — "
            "it should only exist in the legacy bridge map, not the validation set."
        )

    def test_build_row_normalises_legacy_alert_level(self):
        sig = {**_MINIMAL_SIG, "alert_level": "WHALE"}  # pre-rearch value
        row = _build_row(sig, _MINIMAL_EP)
        assert row["alert_level"] == "BLOCK", (
            f"Expected WHALE to bridge to BLOCK, got {row['alert_level']!r}"
        )

    def test_build_row_alert_level_unknown_defaults_to_watch(self):
        sig = {**_MINIMAL_SIG, "alert_level": "TOTALLY_UNKNOWN_VALUE"}
        row = _build_row(sig, _MINIMAL_EP)
        assert row["alert_level"] == "WATCH"


# ---------------------------------------------------------------------------
# Direction vocab is REARCH (BULLISH/BEARISH/NEUTRAL), not BUY/SELL/HOLD
# ---------------------------------------------------------------------------

class TestDirectionReArchVocab:

    @pytest.mark.parametrize("legacy,expected", [
        ("BUY",   "BULLISH"),
        ("SELL",  "BEARISH"),
        ("HOLD",  "NEUTRAL"),
    ])
    def test_legacy_direction_bridges_correctly(self, legacy, expected):
        assert _normalise_direction(legacy) == expected

    @pytest.mark.parametrize("rearch", ["BULLISH", "BEARISH", "NEUTRAL"])
    def test_rearch_direction_passes_through(self, rearch):
        assert _normalise_direction(rearch) == rearch

    def test_build_row_emits_rearch_direction(self):
        sig = {**_MINIMAL_SIG, "direction": "BUY"}
        row = _build_row(sig)
        assert row["direction"] == "BULLISH"


# ---------------------------------------------------------------------------
# Sentinel: _build_row output only contains known-good columns
# ---------------------------------------------------------------------------

class TestBuildRowColumnWhitelist:
    """
    Fail-safe: if a new retired column is accidentally added back,
    this test will catch it before merge.
    """

    ALLOWED_KEYS = {
        "ticker", "recommendation", "composite_score", "reasoning",
        "alert_level", "direction", "sentiment", "premium", "trade_type",
        "contract_type", "total_premium", "trade_count", "is_accelerating",
        "signal_ts",
    }

    def test_no_unexpected_keys_in_build_row_output(self):
        row = _build_row(_MINIMAL_SIG, _MINIMAL_EP)
        extra = set(row.keys()) - self.ALLOWED_KEYS
        assert not extra, (
            f"_build_row() emitted unexpected keys: {extra}. "
            "If you added a new column, add it to ALLOWED_KEYS in this test "
            "AND confirm the migration adds it to signal_history first."
        )
