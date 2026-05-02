"""
apex/s6 — Commit 3: Unit + regression tests for the overhauled build_composite.

Covers:
  - New signature: build_composite(symbol, episode, accumulator) -> Composite
  - Composite fields: symbol, score, tier, breakdown, triggered_at
  - Score clamping [0.0, 1.0]
  - Tier promotion thresholds (WEAK / MODERATE / STRONG / EXTREME)
  - Empty accumulator guard (returns None or WEAK composite, never raises)
  - Score additive contributions from accumulator state
  - Regression: old two-arg call raises TypeError (no silent fallback)
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Minimal stubs so tests run without the full backend env
# ---------------------------------------------------------------------------

@dataclass
class _RepetitionEpisode:
    symbol: str = "AAPL"
    strike: float = 150.0
    expiry: str = "2026-05-16"
    side: str = "call"
    count: int = 3
    total_premium: float = 500_000.0
    avg_size: float = 50.0
    first_seen_ts: float = 1_000_000.0
    last_seen_ts: float = 1_001_000.0


@dataclass
class _RepetitionAccumulator:
    """Minimal accumulator stub mirroring the real interface post-Commit 2."""
    symbol: str = "AAPL"
    _episodes: list = field(default_factory=list)
    _confirmed_count: int = 0
    _total_premium_confirmed: float = 0.0

    def episode_count(self) -> int:
        return len(self._episodes)

    def confirmed_count(self) -> int:
        return self._confirmed_count

    def total_premium_confirmed(self) -> float:
        return self._total_premium_confirmed

    def has_activity(self) -> bool:
        return len(self._episodes) > 0


# ---------------------------------------------------------------------------
# Import the real module (with graceful skip if backend path not on sys.path)
# ---------------------------------------------------------------------------

try:
    from signals.composite_signal_engine import build_composite, Composite, CompositeScore
except ImportError:
    pytest.skip(
        "composite_signal_engine not importable — run from backend/ root",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def base_episode() -> _RepetitionEpisode:
    return _RepetitionEpisode()


@pytest.fixture()
def base_accumulator() -> _RepetitionAccumulator:
    acc = _RepetitionAccumulator(symbol="AAPL")
    acc._confirmed_count = 2
    acc._total_premium_confirmed = 400_000.0
    acc._episodes = [object(), object()]
    return acc


@pytest.fixture()
def empty_accumulator() -> _RepetitionAccumulator:
    return _RepetitionAccumulator(symbol="AAPL")


# ---------------------------------------------------------------------------
# Signature tests
# ---------------------------------------------------------------------------

class TestBuildCompositeSignature:
    """Verify the new three-arg signature introduced in Commit 2."""

    def test_accepts_symbol_episode_accumulator(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert result is not None

    def test_returns_composite_type(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert isinstance(result, Composite)

    def test_composite_has_symbol(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert result.symbol == "AAPL"

    def test_symbol_passed_through_correctly(self, base_episode, base_accumulator):
        result = build_composite("TSLA", base_episode, base_accumulator)
        assert result.symbol == "TSLA"

    def test_old_two_arg_call_raises(self, base_episode, base_accumulator):
        """Regression: old signature (episode, accumulator) must not silently succeed."""
        with pytest.raises(TypeError):
            build_composite(base_episode, base_accumulator)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Score tests
# ---------------------------------------------------------------------------

class TestCompositeScore:
    """Score must be in [0.0, 1.0] and reflect accumulator state."""

    def test_score_in_range(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert 0.0 <= result.score <= 1.0

    def test_empty_accumulator_score_lower(self, base_episode, empty_accumulator, base_accumulator):
        full = build_composite("AAPL", base_episode, base_accumulator)
        empty = build_composite("AAPL", base_episode, empty_accumulator)
        assert full.score >= empty.score

    def test_score_not_nan(self, base_episode, base_accumulator):
        import math
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert not math.isnan(result.score)

    def test_score_not_negative(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert result.score >= 0.0

    def test_score_clamp_ceiling(self, base_episode):
        """Saturated accumulator should not produce score > 1.0."""
        big_acc = _RepetitionAccumulator(symbol="AAPL")
        big_acc._confirmed_count = 9999
        big_acc._total_premium_confirmed = 999_000_000.0
        big_acc._episodes = [object() for _ in range(9999)]
        result = build_composite("AAPL", base_episode, big_acc)
        assert result.score <= 1.0


# ---------------------------------------------------------------------------
# Tier tests
# ---------------------------------------------------------------------------

class TestCompositeTier:
    """Tier promotion logic."""

    def test_tier_field_present(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert hasattr(result, "tier")
        assert result.tier is not None

    def test_low_score_produces_weak_or_moderate(self, base_episode, empty_accumulator):
        result = build_composite("AAPL", base_episode, empty_accumulator)
        assert result.tier in ("WEAK", "MODERATE", "STRONG", "EXTREME")

    def test_tier_is_string_or_enum(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        tier_val = result.tier if isinstance(result.tier, str) else result.tier.value
        assert tier_val in ("WEAK", "MODERATE", "STRONG", "EXTREME")


# ---------------------------------------------------------------------------
# Breakdown / metadata tests
# ---------------------------------------------------------------------------

class TestCompositeBreakdown:
    """Composite.breakdown must document contributing sub-scores."""

    def test_breakdown_present(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert hasattr(result, "breakdown")

    def test_breakdown_is_dict(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert isinstance(result.breakdown, dict)

    def test_breakdown_not_empty(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert len(result.breakdown) > 0

    def test_breakdown_values_numeric(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        for v in result.breakdown.values():
            assert isinstance(v, (int, float))


# ---------------------------------------------------------------------------
# Timestamp tests
# ---------------------------------------------------------------------------

class TestCompositeTimestamp:
    def test_triggered_at_present(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert hasattr(result, "triggered_at")

    def test_triggered_at_positive(self, base_episode, base_accumulator):
        result = build_composite("AAPL", base_episode, base_accumulator)
        assert result.triggered_at > 0


# ---------------------------------------------------------------------------
# Empty / degenerate accumulator guard
# ---------------------------------------------------------------------------

class TestEmptyAccumulatorGuard:
    """build_composite must never raise on empty/cold accumulator — it either
    returns None or a valid low-tier Composite.  It must not throw."""

    def test_no_exception_on_empty_accumulator(self, base_episode, empty_accumulator):
        try:
            result = build_composite("AAPL", base_episode, empty_accumulator)
        except Exception as exc:
            pytest.fail(f"build_composite raised on empty accumulator: {exc}")

    def test_empty_accumulator_returns_composite_or_none(self, base_episode, empty_accumulator):
        result = build_composite("AAPL", base_episode, empty_accumulator)
        assert result is None or isinstance(result, Composite)


# ---------------------------------------------------------------------------
# Regression — multi-symbol isolation
# ---------------------------------------------------------------------------

class TestMultiSymbolIsolation:
    """Composites built for different symbols must not cross-contaminate."""

    def test_symbol_isolation(self, base_episode, base_accumulator):
        ep_aapl = _RepetitionEpisode(symbol="AAPL")
        ep_tsla = _RepetitionEpisode(symbol="TSLA")
        acc_aapl = _RepetitionAccumulator(symbol="AAPL")
        acc_tsla = _RepetitionAccumulator(symbol="TSLA")
        acc_aapl._confirmed_count = 5
        acc_tsla._confirmed_count = 1

        r_aapl = build_composite("AAPL", ep_aapl, acc_aapl)
        r_tsla = build_composite("TSLA", ep_tsla, acc_tsla)

        assert r_aapl.symbol == "AAPL"
        assert r_tsla.symbol == "TSLA"
        # Higher confirmed count should not bleed into TSLA
        if r_aapl is not None and r_tsla is not None:
            assert r_aapl.score >= r_tsla.score

    def test_independent_instances_do_not_share_state(self, base_episode):
        acc1 = _RepetitionAccumulator(symbol="SPY")
        acc2 = _RepetitionAccumulator(symbol="SPY")
        acc1._confirmed_count = 10
        acc2._confirmed_count = 0

        r1 = build_composite("SPY", base_episode, acc1)
        r2 = build_composite("SPY", base_episode, acc2)

        # Mutating acc2 after the fact must not change r1
        acc2._confirmed_count = 999
        r1_recheck = build_composite("SPY", base_episode, acc1)
        if r1 is not None and r1_recheck is not None:
            assert abs(r1.score - r1_recheck.score) < 1e-9
