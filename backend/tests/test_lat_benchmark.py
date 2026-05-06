"""
tests/test_lat_benchmark.py

QA-2: LAT benchmark — p99 latency of _process_trade() hot path.

CI safety:
  This test is SKIPPED in CI by default. Set the env variable:
    CI_SKIP_LAT_BENCHMARK=1
  in your CircleCI config to prevent non-deterministic false-positive
  failures caused by shared-runner CPU contention.

  To run locally:
    pytest tests/test_lat_benchmark.py -v
  Or with explicit threshold override:
    LAT_P99_THRESHOLD_MS=10 pytest tests/test_lat_benchmark.py -v

Design:
  - Drives _process_trade() with a fully mocked accumulator, bus, dedup,
    persist_flow_event, persist_flow_episode, and parse_tradier_trade.
  - No network, no DB, no asyncio.sleep — pure Python hot-path overhead.
  - 1000 iterations. p99 (990th percentile) must be < LAT_P99_THRESHOLD_MS.
  - Uses wall-clock time.perf_counter() for per-call timing.

Test index:
  LAT-1  p99_process_trade_under_threshold
         1000 calls to _process_trade(); p99 < threshold (default 5ms).
"""
import asyncio
import datetime
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# CI guard
# ---------------------------------------------------------------------------
SKIP_LAT = os.environ.get("CI_SKIP_LAT_BENCHMARK", "").strip() in ("1", "true", "yes")
LAT_P99_THRESHOLD_MS = float(os.environ.get("LAT_P99_THRESHOLD_MS", "5"))

pytestmark = pytest.mark.benchmark


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

def _make_mock_ev():
    ev = MagicMock()
    ev.ticker         = "AAPL"
    ev.contract_type  = "CALL"
    ev.strike         = 150.0
    ev.expiry         = "2026-06-20"
    ev.premium        = 80_000.0
    ev.dte            = 30
    ev.size           = 100
    ev.fill_price     = 2.50
    ev.bid            = 2.45
    ev.ask            = 2.55
    ev.trade_type     = "BTO"
    ev.bid_ask_class  = "AT_ASK"
    ev.is_aggressive  = True
    ev.is_golden_sweep = False
    ev.sentiment      = "BULLISH"
    ev.influence_tier = "INSTITUTIONAL"
    ev.conviction_score = 0.75
    ev.exchange_count = 1
    ev.fill_count     = 1
    ev.open_interest  = 5000
    ev.iv             = 0.35
    ev.underlying_price = 148.0
    ev.is_synthetic_quote = False
    ev.timestamp      = datetime.datetime(2026, 5, 4, 14, 0, 0)
    ev.order_side     = "UNKNOWN"
    ev.execution_mechanic = "AMBIGUOUS_LONG"
    return ev


def _make_mock_sig_ep():
    ep = MagicMock()
    ep.ticker         = "AAPL"
    ep.contract_type  = "CALL"
    ep.strike         = 150.0
    ep.expiry         = "2026-06-20"
    ep.trade_count    = 5
    ep.total_premium  = 200_000.0
    ep.is_accelerating = False
    ep.dominant_direction = "REPEAT_BUY"
    return ep


# ---------------------------------------------------------------------------
# LAT-1
# ---------------------------------------------------------------------------

@pytest.mark.skipif(SKIP_LAT, reason="CI_SKIP_LAT_BENCHMARK=1 — set to run locally only")
def test_lat1_p99_process_trade_under_threshold():
    """
    LAT-1: p99 of 1000 _process_trade() calls must be < LAT_P99_THRESHOLD_MS.

    All I/O (DB, bus, network) is mocked. Measures pure Python hot-path
    overhead: parse gate, dedup gate, accumulator gate, cache lookup,
    persist dispatch, debounce logic.
    """
    import services.tradier_stream as ts

    N = 1000
    ev       = _make_mock_ev()
    sig_ep   = _make_mock_sig_ep()
    mock_lbc = MagicMock()
    mock_lbc.get = MagicMock(return_value=None)  # cold cache — worst-case path

    ts._signal_last_emit.clear()
    ts._lookback_result_cache.clear()
    ts._stats["ticks"] = 0

    raw = {"type": "timesale", "timesale": {
        "symbol": "AAPL260620C00150000",
        "last": "2.50", "bid": "2.45", "ask": "2.55",
        "size": "100", "exch": "C",
    }}

    timings: list[float] = []

    async def run_benchmark():
        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.persist_flow_event", new=AsyncMock()), \
             patch("services.tradier_stream.persist_flow_episode", new=AsyncMock()), \
             patch("services.tradier_stream.enqueue_lookback"), \
             patch("services.tradier_stream.bus") as mock_bus, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.is_directionally_aggressive", return_value=False), \
             patch("services.tradier_stream._lbc", mock_lbc), \
             patch("services.tradier_stream._lbc_fresh", return_value=False):

            mock_bus.publish_all = AsyncMock()
            mock_acc.ingest_tick       = AsyncMock(return_value=sig_ep)
            mock_acc.get_signal        = AsyncMock(return_value=sig_ep)
            mock_acc.get_alert_level   = MagicMock(return_value="WATCH")
            mock_acc._multi_day_min_days = 2
            mock_dedup.is_duplicate    = MagicMock(return_value=False)
            mock_dedup.is_sweep        = MagicMock(return_value=False)

            for _ in range(N):
                t0 = time.perf_counter()
                await ts._process_trade(raw)
                timings.append((time.perf_counter() - t0) * 1000)  # ms

    # asyncio.run() is the correct pattern in Python 3.10+ —
    # get_event_loop() raises RuntimeError when no loop exists in the thread.
    asyncio.run(run_benchmark())

    timings.sort()
    p99_ms = timings[int(N * 0.99) - 1]
    p50_ms = timings[N // 2]

    print(f"\nLAT p50={p50_ms:.3f}ms  p99={p99_ms:.3f}ms  threshold={LAT_P99_THRESHOLD_MS}ms")

    assert p99_ms < LAT_P99_THRESHOLD_MS, (
        f"LAT-1 FAILED: p99={p99_ms:.3f}ms exceeds threshold={LAT_P99_THRESHOLD_MS}ms. "
        f"p50={p50_ms:.3f}ms. "
        f"If this is a CI false-positive, set CI_SKIP_LAT_BENCHMARK=1 in CircleCI env vars."
    )
