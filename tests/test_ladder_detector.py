"""
tests/test_ladder_detector.py

S5 — Apex L4: Ladder detection unit tests

Coverage targets (100% line + branch):
  QA-25  — Ladder positive: same ticker, same expiry, 3+ distinct strikes
  QA-26  — Ladder negative: cross-expiry guard prevents false ladder
  -      — Empty input
  -      — One and two episodes (below min_strikes threshold)
  -      — Repeated same strike across multiple episodes does not inflate count
  -      — Stale episode excluded by expires_before
  -      — Stale episode excluded when last_seen is None (treated as not stale)
  -      — Multi-ticker isolation: ladder on one ticker does not mix with another
  -      — min_strikes override (2 strikes threshold)
  -      — total_premium sums all contributing episodes
  -      — tz-naive / tz-aware cutoff compatibility branches
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from repo root without installing the package
# ---------------------------------------------------------------------------
_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from signals.ladder_detector import LadderSignal, detect_ladder
from signals.repetition_accumulator import RepetitionEpisode


# ---------------------------------------------------------------------------
# Fixed time sentinels — all timestamps in tests are relative to NOW so that
# stale-expiry tests are deterministic regardless of when the suite runs.
#
# Timeline:  OLD (NOW-30m) -------- FRESH (NOW-1m) ---- NOW
#
# expires_before=OLD  -> keeps FRESH, keeps NOW-timestamped; excludes nothing
#                        in practice since all sentinels are >= OLD
# expires_before=NOW  -> excludes OLD *and* FRESH (both < NOW)
# expires_before=FRESH-> excludes OLD only (OLD < FRESH); keeps FRESH (equal,
#                        not strictly less) and NOW
#
# _make_ep defaults last_seen to FRESH so episodes without an explicit
# last_seen are always treated as current relative to OLD-based cutoffs.
# ---------------------------------------------------------------------------
NOW   = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
OLD   = NOW - timedelta(minutes=30)
FRESH = NOW - timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ep(
    ticker: str,
    expiry: str,
    strike: float,
    premium: float,
    last_seen: datetime | None = FRESH,
) -> RepetitionEpisode:
    """
    Build a minimal RepetitionEpisode suitable for ladder detection tests.
    total_premium is driven by the events list, so we inject a mock event.

    last_seen defaults to FRESH (NOW - 1 min) so that episodes built without
    an explicit timestamp are always fresh relative to OLD-based cutoffs.
    Pass last_seen=None explicitly to test the None-last_seen path.
    """
    ep = RepetitionEpisode(
        ticker=ticker,
        contract_type="CALL",
        strike=strike,
        expiry=expiry,
    )
    ev = MagicMock()
    ev.premium = premium
    ep.events.append(ev)
    ep.last_seen = last_seen  # assigned directly; None is preserved as-is
    return ep


# ---------------------------------------------------------------------------
# QA-25 — Ladder positive: same ticker, same expiry, 3+ distinct strikes
# ---------------------------------------------------------------------------

class TestLadderPositive:

    def test_three_strikes_same_ticker_same_expiry(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 300_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 250_000),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000),
        ]
        result = detect_ladder(eps)
        assert result is not None
        assert isinstance(result, LadderSignal)
        assert result.ticker == "NVDA"
        assert result.expiry == "2026-06-20"
        assert result.strikes == [580.0, 590.0, 600.0]
        assert result.total_premium == pytest.approx(750_000)

    def test_four_strikes_returns_all_sorted(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 610.0, 100_000),
            _make_ep("NVDA", "2026-06-20", 580.0, 100_000),
            _make_ep("NVDA", "2026-06-20", 600.0, 100_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 100_000),
        ]
        result = detect_ladder(eps)
        assert result is not None
        assert result.strikes == [580.0, 590.0, 600.0, 610.0]
        assert result.total_premium == pytest.approx(400_000)

    def test_total_premium_sums_all_episodes(self):
        eps = [
            _make_ep("AAPL", "2026-07-18", 200.0, 500_000),
            _make_ep("AAPL", "2026-07-18", 210.0, 300_000),
            _make_ep("AAPL", "2026-07-18", 220.0, 200_000),
        ]
        result = detect_ladder(eps)
        assert result is not None
        assert result.total_premium == pytest.approx(1_000_000)


# ---------------------------------------------------------------------------
# QA-26 — Ladder negative: cross-expiry guard
# ---------------------------------------------------------------------------

class TestCrossExpiryGuard:

    def test_same_ticker_different_expiries_no_ladder(self):
        """3 strikes but spread across different expiries — must NOT fire."""
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 300_000),
            _make_ep("NVDA", "2026-07-18", 590.0, 250_000),
            _make_ep("NVDA", "2026-08-15", 600.0, 200_000),
        ]
        result = detect_ladder(eps)
        assert result is None

    def test_two_expiries_first_qualifies_second_does_not(self):
        """One expiry has 3 strikes, the other has 2 — only first fires."""
        eps = [
            _make_ep("TSLA", "2026-06-20", 300.0, 200_000),
            _make_ep("TSLA", "2026-06-20", 310.0, 200_000),
            _make_ep("TSLA", "2026-06-20", 320.0, 200_000),
            _make_ep("TSLA", "2026-07-18", 300.0, 100_000),
            _make_ep("TSLA", "2026-07-18", 310.0, 100_000),
        ]
        result = detect_ladder(eps)
        assert result is not None
        assert result.expiry == "2026-06-20"


# ---------------------------------------------------------------------------
# Below-threshold cases
# ---------------------------------------------------------------------------

class TestBelowThreshold:

    def test_empty_list_returns_none(self):
        assert detect_ladder([]) is None

    def test_one_episode_returns_none(self):
        eps = [_make_ep("NVDA", "2026-06-20", 580.0, 300_000)]
        assert detect_ladder(eps) is None

    def test_two_episodes_returns_none(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 300_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 250_000),
        ]
        assert detect_ladder(eps) is None

    def test_duplicate_same_strike_does_not_count_as_distinct(self):
        """3 episodes but all on the same strike — only 1 distinct strike."""
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 100_000),
            _make_ep("NVDA", "2026-06-20", 580.0, 100_000),
            _make_ep("NVDA", "2026-06-20", 580.0, 100_000),
        ]
        assert detect_ladder(eps) is None

    def test_two_distinct_strikes_two_episodes_returns_none(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000),
        ]
        assert detect_ladder(eps) is None


# ---------------------------------------------------------------------------
# Stale-episode expiry
# ---------------------------------------------------------------------------

class TestStaleEpisodeExpiry:

    def test_all_fresh_episodes_qualify(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000, last_seen=FRESH),
        ]
        result = detect_ladder(eps, expires_before=OLD)
        assert result is not None
        assert result.ticker == "NVDA"

    def test_stale_episode_excluded_drops_below_threshold(self):
        """Two fresh + one stale — after excluding stale, only 2 strikes remain."""
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000, last_seen=OLD),
        ]
        result = detect_ladder(eps, expires_before=NOW)
        assert result is None

    def test_episode_with_none_last_seen_not_filtered(self):
        """
        last_seen=None means no staleness data — episode is always kept.

        expires_before=OLD is used here (not NOW) because FRESH < NOW would
        cause the two FRESH companions to be filtered too, leaving only the
        None episode which alone cannot form a 3-strike ladder.
        With expires_before=OLD: FRESH (NOW-1m) >= OLD (NOW-30m) so companions
        survive; OLD episodes would be cut but none exist here.
        """
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000, last_seen=None),
        ]
        result = detect_ladder(eps, expires_before=OLD)
        assert result is not None

    def test_no_expires_before_no_stale_filtering(self):
        """expires_before=None means no filtering regardless of last_seen age."""
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000, last_seen=OLD),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000, last_seen=OLD),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000, last_seen=OLD),
        ]
        result = detect_ladder(eps, expires_before=None)
        assert result is not None

    def test_tz_naive_last_seen_with_tz_aware_cutoff(self):
        """Covers the tz-naive ep_ts + tz-aware cutoff compatibility branch."""
        naive_ts = datetime(2026, 5, 1, 11, 0, 0)  # tz-naive, 1 hour before NOW
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000, last_seen=naive_ts),
        ]
        # naive_ts < NOW (both UTC-equivalent) — stale, should be excluded
        result = detect_ladder(eps, expires_before=NOW)
        assert result is None

    def test_tz_aware_last_seen_with_tz_naive_cutoff(self):
        """Covers the tz-aware ep_ts + tz-naive cutoff compatibility branch."""
        naive_cutoff = datetime(2026, 5, 1, 12, 0, 0)  # tz-naive = NOW equivalent
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000, last_seen=FRESH),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000, last_seen=OLD),
        ]
        result = detect_ladder(eps, expires_before=naive_cutoff)
        assert result is None


# ---------------------------------------------------------------------------
# Multi-ticker isolation
# ---------------------------------------------------------------------------

class TestMultiTickerIsolation:

    def test_ladder_on_one_ticker_does_not_mix_with_another(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 300_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 250_000),
            _make_ep("NVDA", "2026-06-20", 600.0, 200_000),
            _make_ep("AAPL", "2026-06-20", 200.0, 100_000),
            _make_ep("AAPL", "2026-06-20", 210.0, 100_000),
        ]
        result = detect_ladder(eps)
        assert result is not None
        assert result.ticker == "NVDA"
        assert "AAPL" not in result.ticker
        assert 200.0 not in result.strikes

    def test_both_tickers_qualify_returns_first_encountered(self):
        """Two qualifying (ticker, expiry) groups — returns first in iteration."""
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 100_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 100_000),
            _make_ep("NVDA", "2026-06-20", 600.0, 100_000),
            _make_ep("AAPL", "2026-06-20", 200.0, 100_000),
            _make_ep("AAPL", "2026-06-20", 210.0, 100_000),
            _make_ep("AAPL", "2026-06-20", 220.0, 100_000),
        ]
        result = detect_ladder(eps)
        assert result is not None
        assert result.ticker in ("NVDA", "AAPL")


# ---------------------------------------------------------------------------
# min_strikes override
# ---------------------------------------------------------------------------

class TestMinStrikesOverride:

    def test_two_strikes_qualifies_with_min_strikes_2(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000),
        ]
        result = detect_ladder(eps, min_strikes=2)
        assert result is not None
        assert result.strikes == [580.0, 590.0]

    def test_default_min_strikes_3_requires_three(self):
        eps = [
            _make_ep("NVDA", "2026-06-20", 580.0, 200_000),
            _make_ep("NVDA", "2026-06-20", 590.0, 200_000),
        ]
        result = detect_ladder(eps)
        assert result is None
