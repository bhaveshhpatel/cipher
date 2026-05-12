"""
test_dedup_clock_c020.py — Regression tests for C-020 (clock mismatch fix).

Bug: _process_trade() passed _time.monotonic() as arrival_ts to
DedupCache.is_duplicate(). DedupCache stores first_seen as time.time()
(wall-clock epoch ~1.77e9). The TTL check (now - first_seen) < 5.0 with
monotonic as 'now' (~8431s) always produced a large negative number, which
is always < 5.0 — meaning every cache entry was PERMANENTLY treated as a
duplicate and TTL expiry never worked.

Fix: arrival_ts = _time.time() in _process_trade().

Tests in this file:
  C020-1  DedupCache TTL expires correctly with wall-clock timestamps
  C020-2  Monotonic timestamp breaks TTL expiry (documents the old bug)
  C020-3  _process_trade passes wall-clock ts to is_duplicate (integration)
  C020-4  Same OCC/size/fill at T=2s is deduped (within TTL)
  C020-5  Same OCC/size/fill at T=10s is NOT deduped (TTL expired)
  C020-6  arrival_ts value is wall-clock (> 1e9), not monotonic (small)
  C020-7  Dedup stats counter increments only on actual duplicates
  C020-8  Regression: existing dedup edge-case tests still pass
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timesale_raw(symbol="TSLA260425C00375000", last=4.25, size=150,
                       bid=4.10, ask=4.40, exch="C"):
    """Build a Tradier timesale envelope as received in _process_trade."""
    return {
        "type": "timesale",
        "timesale": {
            "symbol": symbol,
            "last":   last,
            "bid":    bid,
            "ask":    ask,
            "size":   size,
            "exch":   exch,
        },
    }


# ---------------------------------------------------------------------------
# C020-1: Wall-clock TTL expires correctly
# ---------------------------------------------------------------------------

def test_c020_1_wall_clock_ttl_expires():
    """Entry seeded at T-10s should NOT be a duplicate (TTL=5s)."""
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=5.0)

    occ  = "TSLA260425C00375000"
    size = 150
    fill = 4.25

    past = time.time() - 10.0
    cache.is_duplicate(occ, size, fill, exchange="C", ts=past)

    result = cache.is_duplicate(occ, size, fill, exchange="M", ts=time.time())
    assert result is False, "Entry should have expired after 10s with TTL=5s"


# ---------------------------------------------------------------------------
# C020-2: Documents the old monotonic bug
# ---------------------------------------------------------------------------

def test_c020_2_monotonic_breaks_ttl_documents_old_bug():
    """
    With monotonic as 'now' and wall-clock as first_seen, (now - first_seen)
    is always a large negative number < TTL. This means entries never expire.
    This test documents why the bug existed and what the wrong behavior looks like.
    """
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=5.0)

    occ  = "AAPL260117C00180000"
    size = 100
    fill = 3.50

    past_wall = time.time() - 10.0
    cache.is_duplicate(occ, size, fill, exchange="C", ts=past_wall)

    monotonic_now = time.monotonic()
    result_old_bug = cache.is_duplicate(occ, size, fill, exchange="M", ts=monotonic_now)

    assert result_old_bug is True, (
        "BUG REPRODUCTION: monotonic 'now' vs wall-clock first_seen should "
        "always produce a duplicate (large negative difference < TTL). "
        "This was the C-020 bug."
    )

    cache2 = DedupCache(ttl_seconds=5.0)
    cache2.is_duplicate(occ, size, fill, exchange="C", ts=time.time() - 10.0)
    result_correct = cache2.is_duplicate(occ, size, fill, exchange="M", ts=time.time())
    assert result_correct is False, (
        "FIXED behavior: wall-clock 'now' vs wall-clock first_seen (10s ago) "
        "should correctly expire and NOT be a duplicate"
    )


# ---------------------------------------------------------------------------
# C020-3: _process_trade uses time.time() for arrival_ts (integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c020_3_process_trade_uses_wall_clock_arrival_ts():
    """
    Verify that _process_trade passes a wall-clock timestamp (> 1e9)
    to flow_dedup.is_duplicate(), not a monotonic value (small float).

    The spy accepts **kwargs so it remains forward-proof against new keyword
    args added to the is_duplicate() call site (e.g. tier_int= from ING-010).
    Only the `ts` kwarg is inspected — that is the sole contract being tested.

    Note: _ingestion_processor is patched to pass-through (return ev unchanged)
    so the REARCH-002 DTE gate does not drop the test event before dedup is
    reached. The test symbol (TSLA260425C00375000) has an expiry in the past,
    which would fail Gate 1 (min_dte=1) under real config. Ingestion gate
    correctness is covered by test_rearch002_ingestion_floors.py.
    """
    import services.tradier_stream as ts_module

    captured_ts = []

    def _spy_is_duplicate(occ_symbol, size, fill, exchange=None, ts=None, **kwargs):
        if ts is not None:
            captured_ts.append(ts)
        return False

    def _passthrough_process(ev, tier=None):
        """Return ev unchanged — bypass ingestion gate for this test."""
        return ev

    raw = _make_timesale_raw()

    with patch.object(ts_module.flow_dedup, "is_duplicate", side_effect=_spy_is_duplicate), \
         patch.object(ts_module._ingestion_processor, "process", side_effect=_passthrough_process), \
         patch.object(ts_module, "persist_flow_event", new_callable=AsyncMock), \
         patch.object(ts_module, "bus") as mock_bus:
        mock_bus.publish_all = AsyncMock()
        await ts_module._process_trade(raw)

    assert len(captured_ts) == 1, "is_duplicate should have been called exactly once"
    ts_val = captured_ts[0]

    assert ts_val > 1_000_000_000, (
        f"C-020: arrival_ts should be wall-clock (> 1e9) but got {ts_val:.2f}. "
        f"If this fails, _time.monotonic() is still being used instead of _time.time()."
    )


# ---------------------------------------------------------------------------
# C020-4: Same OCC/size/fill within TTL IS a duplicate
# ---------------------------------------------------------------------------

def test_c020_4_within_ttl_is_duplicate():
    """Two identical prints 2s apart (TTL=5s) — second should be deduped."""
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=5.0)

    now = time.time()
    occ, size, fill = "NVDA260620C00900000", 200, 12.50

    cache.is_duplicate(occ, size, fill, exchange="C", ts=now)
    result = cache.is_duplicate(occ, size, fill, exchange="M", ts=now + 2.0)

    assert result is True, "Same print 2s later should be deduplicated (TTL=5s)"


# ---------------------------------------------------------------------------
# C020-5: Same OCC/size/fill after TTL NOT a duplicate
# ---------------------------------------------------------------------------

def test_c020_5_after_ttl_not_duplicate():
    """Same OCC/size/fill 10s apart (TTL=5s) — should pass as new canonical."""
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=5.0)

    now = time.time()
    occ, size, fill = "SPY260620P00500000", 500, 2.10

    cache.is_duplicate(occ, size, fill, exchange="C", ts=now)
    result = cache.is_duplicate(occ, size, fill, exchange="C", ts=now + 10.0)

    assert result is False, (
        "Same OCC/size/fill 10s later should NOT be a duplicate (TTL=5s expired). "
        "This is Clink-4: the old monotonic bug made this always return True."
    )


# ---------------------------------------------------------------------------
# C020-6: Confirm arrival_ts magnitude matches wall-clock, not monotonic
# ---------------------------------------------------------------------------

def test_c020_6_wall_clock_magnitude():
    """
    Sanity check: time.time() is epoch-scale (> 1e9).
    time.monotonic() is uptime-scale (seconds since some arbitrary point).

    The invariants that are always true regardless of machine uptime:
      - wall > 1e9          (UNIX epoch is currently ~1.78e9)
      - wall > mono         (epoch always exceeds uptime in seconds)
      - mono < 1e7          (< 115 days uptime; reasonable CI/dev box bound)

    We do NOT assert wall > mono * 1000 because that breaks on machines
    with uptime > ~11.5 days (monotonic > 1e6s → mono*1000 > 1e9 > wall).
    The meaningful distinction — wall is epoch-scale, mono is not — is
    fully captured by `wall > 1e9` and `mono < 1e7`.
    """
    import time as t
    wall = t.time()
    mono = t.monotonic()

    assert wall > 1_000_000_000, (
        f"time.time() should be epoch-scale (> 1e9), got {wall:.0f}"
    )
    assert wall > mono, (
        f"time.time() ({wall:.0f}) should always exceed time.monotonic() ({mono:.1f}) "
        f"in absolute magnitude — epoch seconds > uptime seconds."
    )
    assert mono < 10_000_000, (
        f"time.monotonic() ({mono:.1f}s) exceeds 115 days of uptime — "
        f"unexpected in a CI or dev environment. Clock source may be wrong."
    )


# ---------------------------------------------------------------------------
# C020-7: Dedup stats counter increments only on true duplicates
# ---------------------------------------------------------------------------

def test_c020_7_dedup_stats_count_correct():
    """After fix: canonical + 2 duplicates → dedup_duplicates=2, dedup_seen=1."""
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=5.0)

    now = time.time()
    occ, size, fill = "MSFT260117C00400000", 75, 8.00

    cache.is_duplicate(occ, size, fill, exchange="C", ts=now)
    cache.is_duplicate(occ, size, fill, exchange="M", ts=now + 1.0)
    cache.is_duplicate(occ, size, fill, exchange="X", ts=now + 3.0)

    stats = cache.dedup_stats()
    assert stats["dedup_seen"]       == 1, f"Expected 1 canonical, got {stats['dedup_seen']}"
    assert stats["dedup_duplicates"] == 2, f"Expected 2 duplicates, got {stats['dedup_duplicates']}"


# ---------------------------------------------------------------------------
# C020-8: Regression — existing edge-case tests still hold
# ---------------------------------------------------------------------------

def test_c020_8_regression_first_event_not_duplicate():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=60)
    result = cache.is_duplicate("AAPL260620C00200000", 100, 5.0, exchange="C", ts=time.time())
    assert result is False


def test_c020_8_regression_different_size_not_duplicate():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=60)
    now = time.time()
    cache.is_duplicate("AAPL260620C00200000", 100, 5.0, exchange="C", ts=now)
    result = cache.is_duplicate("AAPL260620C00200000", 200, 5.0, exchange="C", ts=now + 1.0)
    assert result is False, "Different size = different trade = should NOT be deduped"


def test_c020_8_regression_sweep_detection_still_works():
    """3 exchanges within sweep_window → is_sweep() returns True."""
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=5.0, sweep_window=8.0, sweep_min_exchanges=3)
    now = time.time()
    occ, size, fill = "QQQ260620C00450000", 300, 6.75

    cache.is_duplicate(occ, size, fill, exchange="C", ts=now)
    cache.is_duplicate(occ, size, fill, exchange="M", ts=now + 1.5)
    cache.is_duplicate(occ, size, fill, exchange="X", ts=now + 3.0)

    assert cache.is_sweep(occ, size, fill) is True, (
        "3 exchanges within sweep_window should trigger sweep detection"
    )
    assert cache.get_exchange_count(occ, size, fill) == 3
