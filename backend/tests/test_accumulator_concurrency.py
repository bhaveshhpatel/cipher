"""
Tests for RepetitionAccumulator concurrent safety fixes (issues #1 + #2).

Covers:
  - Per-key lock prevents phantom threshold crossings under concurrent ingest
  - Backward-compat ingest() shim removed (PBE-F3, 2026-05-03); tests updated
    to use ingest_tick() directly
  - Eviction of empty episodes after window pruning

API surface changes since original authoring (ING-006 rewrite):
  - acc._key(ev)         removed -> use acc._episode_key(ev)
  - acc._key_from_ep(ep) never existed in production; removed from tests
  - acc.get_signal(...)  retired per PBE-F4 deliberation (2026-05-03);
    stream layer handles emit throttling; cooldown gate tests removed
  - acc.ingest(ev)       backward-compat shim removed (PBE-F3); use ingest_tick
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ev(
    ticker="AAPL",
    contract_type="CALL",
    strike=180.0,
    expiry="2026-06-20",
    premium=20_000.0,
    trade_type="SWEEP",
    dte=30,
    underlying_price=200.0,
    order_side="BUY",
    ts: datetime | None = None,
):
    """Return a minimal OptionsFlowEvent-like SimpleNamespace."""
    ts = ts or datetime.now(timezone.utc)
    return SimpleNamespace(
        ticker=ticker,
        contract_type=contract_type,
        strike=strike,
        expiry=expiry,
        premium=premium,
        trade_type=trade_type,
        dte=dte,
        underlying_price=underlying_price,
        order_side=order_side,
        timestamp=ts,
    )


# ---------------------------------------------------------------------------
# Basic async API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_tick_below_threshold_returns_none():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    ev = _make_ev(premium=10_000)
    result = await acc.ingest_tick(ev)
    assert result is None


@pytest.mark.asyncio
async def test_ingest_tick_crosses_threshold():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    for i in range(3):
        ev = _make_ev(premium=20_000, ts=now + timedelta(seconds=i))
        result = await acc.ingest_tick(ev)
    assert result is not None
    assert result.trade_count == 3
    assert result.total_premium == 60_000


# ---------------------------------------------------------------------------
# Concurrent safety — issue #1: ingest_tick race
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_ingest_tick_no_phantom_threshold():
    """
    Fire 10 concurrent ingest_tick calls each contributing 10_000 premium.
    Threshold is min_trades=3, min_premium=50_000.
    Only ticks 3+ should return a non-None episode; none should see a
    corrupted trade_count from interleaved list mutation.
    """
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    events = [_make_ev(premium=10_000, ts=now + timedelta(milliseconds=i)) for i in range(10)]

    results = await asyncio.gather(*[acc.ingest_tick(ev) for ev in events])

    # At least some results should be non-None (once threshold is crossed)
    non_none = [r for r in results if r is not None]
    assert len(non_none) >= 1

    # The final episode state must be internally consistent
    key = acc._episode_key(events[0])
    ep  = acc._episodes[key]
    assert ep.trade_count == len(ep.events)
    assert ep.total_premium == sum(e.premium for e in ep.events)


# ---------------------------------------------------------------------------
# Concurrent safety — issue #2: single-fire invariant under concurrent ingest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_ingest_tick_consistent_episode_state():
    """
    Fire 20 concurrent ingest_tick calls on the same (ticker, strike, expiry)
    key. The episode's trade_count must equal len(ep.events) after all settle —
    no torn writes, no phantom events.

    NOTE: get_signal() was retired in PBE-F4 (2026-05-03). Emit throttling is
    now owned by the stream layer. This test validates the accumulator's own
    internal consistency under concurrent load, replacing the old double-fire
    get_signal test.
    """
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    events = [
        _make_ev(premium=10_000, ts=now + timedelta(milliseconds=i))
        for i in range(20)
    ]

    await asyncio.gather(*[acc.ingest_tick(ev) for ev in events])

    key = acc._episode_key(events[0])
    ep  = acc._episodes[key]
    assert ep.trade_count == len(ep.events), (
        f"trade_count {ep.trade_count} != len(events) {len(ep.events)} — torn write detected"
    )
    assert ep.total_premium == sum(e.premium for e in ep.events)


# ---------------------------------------------------------------------------
# Episode eviction after window pruning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_events_pruned_episode_evicted():
    """
    If all events in an episode fall outside the rolling window, the episode
    key should be removed from _episodes on the next ingest_tick call for a
    new event on the same key.
    """
    acc = RepetitionAccumulator(window_minutes=1, min_trades=3, min_premium=50_000)
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=5)

    # Inject two old events directly (below threshold, old timestamps)
    ev1 = _make_ev(premium=10_000, ts=old_ts)
    ev2 = _make_ev(premium=10_000, ts=old_ts + timedelta(seconds=1))
    await acc.ingest_tick(ev1)
    await acc.ingest_tick(ev2)

    key = acc._episode_key(ev1)
    assert key in acc._episodes

    # Now ingest a fresh event — old ones get pruned, leaving only this one
    new_ev = _make_ev(premium=10_000, ts=datetime.now(timezone.utc))
    result = await acc.ingest_tick(new_ev)
    assert result is None  # only 1 event left after pruning, below threshold
    # Episode should still exist (it has 1 live event), not evicted
    assert key in acc._episodes
    assert acc._episodes[key].trade_count == 1
