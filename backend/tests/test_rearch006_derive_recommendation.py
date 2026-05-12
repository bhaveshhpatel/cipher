# ============================================================================
# tests/test_rearch006_derive_recommendation.py
#
# REARCH-006 — Unit tests for _derive_recommendation()
#
# All 5 enum outputs are covered: BUY_CALLS, BUY_PUTS, FOLLOW_SWEEP,
# WATCH, NO_ACTION.
#
# Tests are deterministic, pure, and synchronous:
#   - No DB, no async, no network
#   - Episode inputs are plain SimpleNamespace dicts (no RepetitionEpisode)
#   - _derive_recommendation() is a pure function with no external deps
#
# Coverage requirements (QA deliberation):
#   1. All 5 enum values produced at least once
#   2. Ask-side hard gate: conviction >= 3 + ask_side_failed → WATCH
#   3. FOLLOW_SWEEP: conviction == 5 + ask_side_confirmed
#   4. FOLLOW_SWEEP NOT triggered when conviction == 5 + ask_side_failed
#   5. NO_ACTION on conviction == 0
#   6. NO_ACTION on NEUTRAL direction regardless of conviction
#   7. BUY_CALLS on BULLISH + conviction >= 3 + confirmed
#   8. BUY_PUTS on BEARISH + conviction >= 3 + confirmed
#   9. WATCH on conviction 1–2 with confirmed ask-side
#  10. build_signal_row() wires recommendation correctly end-to-end
# ============================================================================

import pytest
from types import SimpleNamespace
from unittest.mock import patch

# Import the private function and the build helper directly.
# _derive_recommendation is module-private by convention (leading underscore)
# but is importable for unit testing.
from signals.signal_engine import (
    _derive_recommendation,
    build_signal_row,
    _VALID_RECOMMENDATIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ep(**kwargs) -> SimpleNamespace:
    """Build a minimal episode-like SimpleNamespace for build_signal_row tests."""
    defaults = dict(
        symbol="AAPL",
        total_premium=100_000.0,
        trade_count=5,
        ask_side_pct=0.75,
        vol_oi_ratio=2.5,
        notional_tier="BLOCK",
        dte_bucket="8-30",
        vol_oi_signal=True,
        contract_type="call",
        episode_id="ep-abc-123",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


_CFG_DEFAULTS = {
    "sig.ask_side_pct_floor": 0.6,
    "sig.min_trade_count": 2,
    "sig.steamroom_score_floor": 3,
}


# ============================================================================
# _derive_recommendation — direct unit tests
# ============================================================================

class TestDeriveRecommendationNoAction:
    """NO_ACTION branch: conviction==0 OR neutral/unknown direction."""

    def test_conviction_zero_bullish(self):
        assert _derive_recommendation(0, "BULLISH", True) == "NO_ACTION"

    def test_conviction_zero_bearish(self):
        assert _derive_recommendation(0, "BEARISH", False) == "NO_ACTION"

    def test_conviction_zero_neutral(self):
        assert _derive_recommendation(0, "NEUTRAL", True) == "NO_ACTION"

    def test_neutral_direction_high_conviction(self):
        # NEUTRAL direction → NO_ACTION regardless of conviction or ask-side.
        assert _derive_recommendation(5, "NEUTRAL", True) == "NO_ACTION"

    def test_neutral_direction_mid_conviction(self):
        assert _derive_recommendation(3, "NEUTRAL", False) == "NO_ACTION"

    def test_unknown_direction_treated_as_no_action(self):
        # Any direction string outside BULLISH/BEARISH is treated as neutral.
        assert _derive_recommendation(4, "SIDEWAYS", True) == "NO_ACTION"


class TestDeriveRecommendationFollowSweep:
    """FOLLOW_SWEEP: conviction==5 AND ask_side_confirmed."""

    def test_bullish_golden_confirmed(self):
        assert _derive_recommendation(5, "BULLISH", True) == "FOLLOW_SWEEP"

    def test_bearish_golden_confirmed(self):
        # FOLLOW_SWEEP is direction-agnostic — it overrides BUY_PUTS.
        assert _derive_recommendation(5, "BEARISH", True) == "FOLLOW_SWEEP"

    def test_golden_unconfirmed_ask_side_not_follow_sweep(self):
        # conviction==5 but ask-side failed → falls through to WATCH.
        # This is the QA hard-gate edge case.
        result = _derive_recommendation(5, "BULLISH", False)
        assert result == "WATCH"
        assert result != "FOLLOW_SWEEP"

    def test_golden_unconfirmed_bearish(self):
        result = _derive_recommendation(5, "BEARISH", False)
        assert result == "WATCH"


class TestDeriveRecommendationBuyCalls:
    """BUY_CALLS: BULLISH + conviction >= 3 + ask_side_confirmed."""

    def test_bullish_conviction_3_confirmed(self):
        assert _derive_recommendation(3, "BULLISH", True) == "BUY_CALLS"

    def test_bullish_conviction_4_confirmed(self):
        assert _derive_recommendation(4, "BULLISH", True) == "BUY_CALLS"

    def test_bullish_conviction_3_not_confirmed(self):
        # Ask-side hard gate: conviction >= 3 but unconfirmed → WATCH.
        result = _derive_recommendation(3, "BULLISH", False)
        assert result == "WATCH"
        assert result != "BUY_CALLS"

    def test_bullish_conviction_4_not_confirmed(self):
        # Even conviction==4 with failed ask-side → WATCH.
        result = _derive_recommendation(4, "BULLISH", False)
        assert result == "WATCH"


class TestDeriveRecommendationBuyPuts:
    """BUY_PUTS: BEARISH + conviction >= 3 + ask_side_confirmed."""

    def test_bearish_conviction_3_confirmed(self):
        assert _derive_recommendation(3, "BEARISH", True) == "BUY_PUTS"

    def test_bearish_conviction_4_confirmed(self):
        assert _derive_recommendation(4, "BEARISH", True) == "BUY_PUTS"

    def test_bearish_conviction_3_not_confirmed(self):
        result = _derive_recommendation(3, "BEARISH", False)
        assert result == "WATCH"
        assert result != "BUY_PUTS"

    def test_bearish_conviction_4_not_confirmed(self):
        result = _derive_recommendation(4, "BEARISH", False)
        assert result == "WATCH"


class TestDeriveRecommendationWatch:
    """WATCH: low conviction, OR conviction >= 3 with unconfirmed ask-side."""

    def test_conviction_1_bullish_confirmed(self):
        assert _derive_recommendation(1, "BULLISH", True) == "WATCH"

    def test_conviction_2_bearish_confirmed(self):
        assert _derive_recommendation(2, "BEARISH", True) == "WATCH"

    def test_conviction_1_bullish_not_confirmed(self):
        assert _derive_recommendation(1, "BULLISH", False) == "WATCH"

    def test_conviction_2_bearish_not_confirmed(self):
        assert _derive_recommendation(2, "BEARISH", False) == "WATCH"

    def test_conviction_3_bullish_ask_failed_is_watch_not_buy_calls(self):
        # Core QA catch: partial-credit episode failing ask-side must be WATCH.
        assert _derive_recommendation(3, "BULLISH", False) == "WATCH"

    def test_conviction_5_bullish_ask_failed_is_watch_not_follow_sweep(self):
        # Perfect score but unconfirmed execution → still WATCH.
        assert _derive_recommendation(5, "BULLISH", False) == "WATCH"


# ============================================================================
# Enum completeness guard
# ============================================================================

class TestEnumCompleteness:
    """All 5 recommendation values are reachable and match the vocab set."""

    def test_all_five_values_producible(self):
        produced = {
            _derive_recommendation(0, "BULLISH", True),       # NO_ACTION
            _derive_recommendation(5, "BULLISH", True),       # FOLLOW_SWEEP
            _derive_recommendation(3, "BULLISH", True),       # BUY_CALLS
            _derive_recommendation(3, "BEARISH", True),       # BUY_PUTS
            _derive_recommendation(2, "BULLISH", True),       # WATCH
        }
        assert produced == {"NO_ACTION", "FOLLOW_SWEEP", "BUY_CALLS", "BUY_PUTS", "WATCH"}
        assert produced == _VALID_RECOMMENDATIONS

    def test_all_outputs_are_in_valid_recommendations_set(self):
        """Exhaustive check: every (score, direction, ask) combo yields a valid enum."""
        for score in range(6):                          # 0–5
            for direction in ("BULLISH", "BEARISH", "NEUTRAL"):
                for ask_confirmed in (True, False):
                    result = _derive_recommendation(score, direction, ask_confirmed)
                    assert result in _VALID_RECOMMENDATIONS, (
                        f"Invalid recommendation {result!r} for "
                        f"score={score}, direction={direction}, ask={ask_confirmed}"
                    )


# ============================================================================
# build_signal_row() — end-to-end wiring of recommendation field
# ============================================================================

class TestBuildSignalRowRecommendationWiring:
    """Verify that build_signal_row() correctly derives and writes recommendation."""

    # Patch get_param and get_effective_premium_threshold so build_signal_row
    # has no real signal_config_store dependency in unit tests.
    _patches = [
        "signals.signal_engine.get_param",
        "signals.signal_engine.get_effective_premium_threshold",
    ]

    def _build(self, ep, alert_level, direction, conviction_score, cfg=None):
        cfg = cfg or _CFG_DEFAULTS
        with patch("signals.signal_engine.get_param", side_effect=lambda k, d=None: _CFG_DEFAULTS.get(k, d)), \
             patch("signals.signal_engine.get_effective_premium_threshold", return_value=50_000.0):
            return build_signal_row(
                ep,
                alert_level=alert_level,
                direction=direction,
                cfg=cfg,
                conviction_score=conviction_score,
            )

    def test_bullish_conviction5_confirmed_gives_follow_sweep(self):
        ep = _ep(ask_side_pct=0.85)  # confirmed
        row = self._build(ep, "GOLDEN", "BULLISH", conviction_score=5)
        assert row["recommendation"] == "FOLLOW_SWEEP"

    def test_bullish_conviction4_confirmed_gives_buy_calls(self):
        ep = _ep(ask_side_pct=0.80)
        row = self._build(ep, "BLOCK", "BULLISH", conviction_score=4)
        assert row["recommendation"] == "BUY_CALLS"

    def test_bearish_conviction3_confirmed_gives_buy_puts(self):
        ep = _ep(ask_side_pct=0.70)
        row = self._build(ep, "NOTEWORTHY", "BEARISH", conviction_score=3)
        assert row["recommendation"] == "BUY_PUTS"

    def test_bullish_conviction4_unconfirmed_gives_watch(self):
        # ask_side_pct below floor → ask_side_confirmed=False → WATCH
        ep = _ep(ask_side_pct=0.40)
        row = self._build(ep, "BLOCK", "BULLISH", conviction_score=4)
        assert row["recommendation"] == "WATCH"

    def test_conviction2_gives_watch(self):
        ep = _ep(ask_side_pct=0.90)
        row = self._build(ep, "NOTEWORTHY", "BULLISH", conviction_score=2)
        assert row["recommendation"] == "WATCH"

    def test_neutral_direction_gives_no_action(self):
        ep = _ep(ask_side_pct=0.90)
        row = self._build(ep, "NOTEWORTHY", "NEUTRAL", conviction_score=4)
        assert row["recommendation"] == "NO_ACTION"

    def test_conviction_zero_gives_no_action(self):
        ep = _ep(ask_side_pct=0.90)
        row = self._build(ep, "WATCH", "BULLISH", conviction_score=0)
        assert row["recommendation"] == "NO_ACTION"

    def test_recommendation_always_in_valid_set(self):
        """Smoke test: recommendation field is always a valid enum value."""
        for direction in ("BULLISH", "BEARISH", "NEUTRAL"):
            for score in range(6):
                for ask_pct in (0.3, 0.75):
                    ep = _ep(ask_side_pct=ask_pct)
                    row = self._build(ep, "NOTEWORTHY", direction, conviction_score=score)
                    assert row["recommendation"] in _VALID_RECOMMENDATIONS, (
                        f"row recommendation {row['recommendation']!r} not in valid set "
                        f"for direction={direction}, score={score}, ask_pct={ask_pct}"
                    )

    def test_recommendation_present_in_row_keys(self):
        ep = _ep()
        row = self._build(ep, "GOLDEN", "BULLISH", conviction_score=5)
        assert "recommendation" in row
