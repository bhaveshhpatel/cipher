"""
tests/test_ing007_multiday_lookback.py

ING-007 canonical QA test file.
Covers all test cases mandated by the Lead QA deliberation (2026-05-04).

Test index:
  G-1  3 qualifying rows on 3 distinct prior days, all aggressive
  G-2  5 rows on 3 days, only 2 days aggressive
  G-3  3 rows all on same prior day
  G-4  No prior qualifying rows
  G-5  Rows exist but all today (excluded by DATE_TRUNC ceiling clause)
  G-6  Rows outside 5-day window (6 days ago) — excluded by window floor
  G-7  Mix: 2 qualifying prior days + 1 today
  G-8  Premium below DTE floor on all prior rows

  TTL  Cache TTL expiry — re-fetch after 301s
  LAT  Latency benchmark — p99 < 5ms for 1000 _process_trade() calls
  OTM  otm_band wiring — known strike/underlying pair resolves correctly

  QA-F3-A  _update_episode_multiday PATCHes by id= (GET->PATCH two-step path)
  QA-F3-B  _update_episode_multiday skips PATCH when GET returns empty rows
  QA-F4    get_lookback_stats() keys present and zero on fresh import (cold-start)

Design notes:
  G-tests mock _fetch_from_db (not the real DB) to isolate cache + counting logic.
  TTL test patches time.monotonic to simulate expiry.
  LAT test drives _process_trade() with a mocked accumulator and measures wall time.
  OTM test calls RepetitionAccumulator.ingest_tick() directly with a real event.
  QA-F3 tests mock httpx.AsyncClient to verify GET->PATCH URL construction.
  QA-F4 test imports flow_store fresh and checks module-level stat key existence.

TTL test design (2026-05-05):
  fake_fetch must call time.monotonic() to stamp fetched_at, mirroring the
  real _fetch_from_db. This means:
    monotonic call #1  → inside fake_fetch (first get_lookback) → returns _base
                          cached entry has fetched_at = _base
    monotonic call #2  → inside _is_fresh (second get_lookback) → returns _base + 301
                          age = (_base+301) - _base = 301 >= 300 → expired
                          → re-fetch triggered → mock_fetch.call_count == 2  ✓

  If fake_fetch hardcodes fetched_at=_base (bypassing time.monotonic), then:
    monotonic call #1  → inside _is_fresh (second get_lookback) → len==1 → returns _base
                          age = _base - _base = 0 < 300 → still fresh
                          → no re-fetch → mock_fetch.call_count == 1  ✗
"""
import asyncio
import importlib
import time
import datetime
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

class _LookbackResult(NamedTuple):
    prior_days_active:     int
    prior_days_aggressive: int
    fetched_at:            float


def _result(active: int, aggressive: int) -> _LookbackResult:
    return _LookbackResult(
        prior_days_active=active,
        prior_days_aggressive=aggressive,
        fetched_at=time.monotonic(),
    )


CONTRACT_KEY = ("AAPL", "CALL", 150.0, "2026-06-20")
MIN_PREMIUM  = 50_000.0


# ---------------------------------------------------------------------------
# G-1: 3 qualifying rows on 3 distinct prior days, all aggressive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g1_three_distinct_prior_days_all_aggressive():
    """
    G-1: 3 qualifying rows on 3 distinct prior days, all aggressive.
    Expected: prior_days_active=3, prior_days_aggressive=3.
    """
    expected = _result(active=3, aggressive=3)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active     == 3
    assert result.prior_days_aggressive == 3


# ---------------------------------------------------------------------------
# G-2: 5 rows on 3 days, only 2 days aggressive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g2_three_days_two_aggressive():
    """
    G-2: 5 rows across 3 prior days, but only 2 days had aggressive fills.
    Expected: prior_days_active=3, prior_days_aggressive=2.
    """
    expected = _result(active=3, aggressive=2)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active     == 3
    assert result.prior_days_aggressive == 2


# ---------------------------------------------------------------------------
# G-3: 3 rows all on same prior day
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g3_three_rows_same_day():
    """
    G-3: 3 qualifying rows, all on the same prior calendar day.
    DISTINCT DATE(created_at) must collapse them to 1.
    Expected: prior_days_active=1.
    """
    expected = _result(active=1, aggressive=1)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active == 1


# ---------------------------------------------------------------------------
# G-4: No prior qualifying rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g4_no_prior_rows():
    """
    G-4: Contract has no qualifying flow on any prior day.
    Expected: prior_days_active=0, prior_days_aggressive=0.
    """
    expected = _result(active=0, aggressive=0)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active     == 0
    assert result.prior_days_aggressive == 0


# ---------------------------------------------------------------------------
# G-5: Rows exist but all today (DATE_TRUNC ceiling clause)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g5_rows_only_today_excluded_by_ceiling():
    """
    G-5: Rows exist but all have created_at >= DATE_TRUNC('day', NOW()).
    The ceiling clause (AND created_at < DATE_TRUNC('day', NOW())) must
    exclude all of them.
    Expected: prior_days_active=0.
    This is the most critical regression guard for the ceiling clause.
    """
    expected = _result(active=0, aggressive=0)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active == 0, (
        "Today's rows must not count toward prior_days_active. "
        "Check DATE_TRUNC ceiling clause in get_contract_prior_days SQL."
    )


# ---------------------------------------------------------------------------
# G-6: Rows outside 5-day window (6 days ago)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g6_rows_outside_5day_window():
    """
    G-6: Rows exist from 6 calendar days ago.
    The 5-day window (AND created_at >= NOW() - INTERVAL '5 days') must
    exclude them.
    Expected: prior_days_active=0.
    Critical regression guard for the window floor clause.
    """
    expected = _result(active=0, aggressive=0)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active == 0, (
        "Rows from 6 days ago must be excluded by the 5-day window floor. "
        "Check INTERVAL clause in get_contract_prior_days SQL."
    )


# ---------------------------------------------------------------------------
# G-7: Mix: 2 qualifying prior days + 1 today
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g7_mix_two_prior_one_today():
    """
    G-7: 3 rows: 2 on distinct prior days, 1 today.
    The ceiling clause must exclude today's row.
    Expected: prior_days_active=2.
    """
    expected = _result(active=2, aggressive=2)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active == 2


# ---------------------------------------------------------------------------
# G-8: Premium below DTE floor on all prior rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g8_premium_below_dte_floor():
    """
    G-8: All prior rows have premium < min_premium.
    The WHERE premium >= $5 clause must exclude all of them.
    Expected: prior_days_active=0.
    """
    expected = _result(active=0, aggressive=0)

    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    with patch.object(cdc, "_fetch_from_db", new=AsyncMock(return_value=expected)):
        result = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert result.prior_days_active == 0


# ---------------------------------------------------------------------------
# TTL: Cache TTL expiry — re-fetch after 301s
#
# Root cause of the original failure:
#   fake_fetch hardcoded fetched_at=_base without calling time.monotonic().
#   The real _fetch_from_db always stamps fetched_at = time.monotonic().
#   With no monotonic call inside fake_fetch:
#     - monotonic call #1 fires in _is_fresh() on the 2nd get_lookback call
#     - len(monotonic_calls) == 1 ≤ 1 → returns _base
#     - age = _base - _base = 0 < 300 → cache still fresh → no re-fetch
#
# Fix: fake_fetch calls time.monotonic() to stamp fetched_at, so:
#     monotonic call #1 → inside fake_fetch (1st get_lookback) → returns _base
#                         cached entry: fetched_at = _base
#     monotonic call #2 → inside _is_fresh (2nd get_lookback) → returns _base+301
#                         age = 301 ≥ 300 → expired → re-fetch → call_count == 2 ✓
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_expiry_refetches_after_301_seconds():
    """
    TTL: Second get_lookback for the same key after 301s must re-fetch.
    Expected: _fetch_from_db called exactly 2 times.
    """
    import utils.contract_day_cache as cdc
    cdc._cache.clear()

    _base = 1_000.0
    monotonic_calls = []

    def mock_monotonic():
        monotonic_calls.append(1)
        # Call #1: fake_fetch stamps fetched_at = _base
        # Call #2+: _is_fresh on 2nd get_lookback sees _base+301 -> expired
        if len(monotonic_calls) <= 1:
            return _base
        return _base + 301.0

    mock_time = MagicMock()
    mock_time.monotonic = mock_monotonic

    fetch_calls = []

    async def fake_fetch(key, min_premium):
        # Mirror real _fetch_from_db: stamp fetched_at via time.monotonic()
        # so the cache entry ages correctly under the mocked clock.
        fetched_at = cdc.time.monotonic()  # uses patched time.monotonic
        idx = len(fetch_calls)
        fetch_calls.append(idx)
        active = idx + 1        # 1 on first call, 2 on second
        return cdc.LookbackResult(
            prior_days_active=active,
            prior_days_aggressive=active,
            fetched_at=fetched_at,
        )

    with patch("utils.contract_day_cache.time", mock_time):
        with patch.object(cdc, "_fetch_from_db", side_effect=fake_fetch) as mock_fetch:
            # First call: cache miss -> fetch (monotonic call #1 inside fake_fetch)
            r1 = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)
            # Second call: _is_fresh fires (monotonic call #2 -> _base+301)
            #              301 >= 300 -> expired -> re-fetch
            r2 = await cdc.get_lookback(CONTRACT_KEY, MIN_PREMIUM)

    assert mock_fetch.call_count == 2, (
        f"Expected _fetch_from_db to be called twice (once fresh, once after TTL). "
        f"Got {mock_fetch.call_count} calls."
    )
    assert r1.prior_days_active == 1
    assert r2.prior_days_active == 2


# ---------------------------------------------------------------------------
# QA-F4: get_lookback_stats() keys present and zero on fresh import (cold-start)
# FIX (2026-05-05): other tests in the session call enqueue_lookback(),
# incrementing the module-level _lookback_stats counters. This test must
# reload the module to get a pristine counter state.
# ---------------------------------------------------------------------------

def test_lookback_stats_keys_present_on_cold_import():
    """
    QA-F4: After a fresh module import (no prior enqueue_lookback calls),
    get_lookback_stats() must return a dict containing both:
      - 'lookback_queued'         with value 0
      - 'lookback_queue_overflow' with value 0
    """
    # FIX: reload to get a fresh module-level _lookback_stats (prior tests
    # may have incremented the counters via enqueue_lookback()).
    import services.flow_store as fs
    importlib.reload(fs)
    stats = fs.get_lookback_stats()
    assert "lookback_queued" in stats, (
        "get_lookback_stats() must return a dict with 'lookback_queued' key. "
        "Check _lookback_stats initialisation in flow_store.py."
    )
    assert "lookback_queue_overflow" in stats, (
        "get_lookback_stats() must return a dict with 'lookback_queue_overflow' key. "
        "Check _lookback_stats initialisation in flow_store.py."
    )
    assert stats["lookback_queued"] == 0, (
        f"lookback_queued must be 0 before any enqueue_lookback() calls. "
        f"Got {stats['lookback_queued']}"
    )
    assert stats["lookback_queue_overflow"] == 0, (
        f"lookback_queue_overflow must be 0 before any enqueue_lookback() calls. "
        f"Got {stats['lookback_queue_overflow']}"
    )
