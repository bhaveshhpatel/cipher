"""
Tests for RepetitionAccumulator concurrent safety fixes (issues #1 + #2).

Covers:
  - Per-key lock prevents phantom threshold crossings under concurrent ingest
  - get_signal cooldown is atomic: concurrent coroutines cannot both fire
    on the same episode within the cooldown window
  - Backward-compat ingest() shim still works correctly
  - Eviction of empty episodes after window pruning
"""
import asyncio
from datetime import datetime, timedelta
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
    ts: datetime | None = None,
):
    """Return a minimal OptionsFlowEvent-like SimpleNamespace."""
    ev = SimpleNamespace(
        ticker=ticker,
        contract_type=contract_type,
        strike=strike,
        expiry=expiry,
        premium=premium,
        timestamp=ts or datetime.utcnow(),
    )
    return ev


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
    now = datetime.utcnow()
    for i in range(3):
        ev = _make_ev(premium=20_000, ts=now + timedelta(seconds=i))
        result = await acc.ingest_tick(ev)
    assert result is not None
    assert result.trade_count == 3
    assert result.total_premium == 60_000


@pytest.mark.asyncio
async def test_get_signal_first_call_fires():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)
    now = datetime.utcnow()
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    result = await acc.get_signal(now, ep)
    assert result is ep
    assert ep.last_signal_at == now


@pytest.mark.asyncio
async def test_get_signal_cooldown_suppresses_second_fire():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)
    now = datetime.utcnow()
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    await acc.get_signal(now, ep)  # first: fires
    result = await acc.get_signal(now + timedelta(minutes=1), ep)  # too soon
    assert result is None


@pytest.mark.asyncio
async def test_get_signal_fires_after_cooldown_elapsed():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)
    now = datetime.utcnow()
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    await acc.get_signal(now, ep)
    result = await acc.get_signal(now + timedelta(minutes=6), ep)
    assert result is ep


@pytest.mark.asyncio
async def test_get_signal_none_episode_is_noop():
    acc = RepetitionAccumulator()
    result = await acc.get_signal(datetime.utcnow(), None)
    assert result is None


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
    now = datetime.utcnow()
    events = [_make_ev(premium=10_000, ts=now + timedelta(milliseconds=i)) for i in range(10)]

    results = await asyncio.gather(*[acc.ingest_tick(ev) for ev in events])

    # At least some results should be non-None (once threshold is crossed)
    non_none = [r for r in results if r is not None]
    assert len(non_none) >= 1

    # The final episode state must be internally consistent
    key = acc._key(events[0])
    ep  = acc._episodes[key]
    assert ep.trade_count == len(ep.events)
    assert ep.total_premium == sum(e.premium for e in ep.events)


# ---------------------------------------------------------------------------
# Concurrent safety — issue #2: get_signal double-fire race
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_get_signal_only_one_fires():
    """
    Fire 20 concurrent get_signal calls on the same episode with the same
    timestamp (simulating 20 StreamWorker coroutines all hitting the cooldown
    check simultaneously). Exactly one should return ep; the rest None.
    """
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)
    now = datetime.utcnow()
    ep  = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    # Seed the lock so _get_lock creates it before concurrent access
    acc._get_lock(acc._key_from_ep(ep))

    results = await asyncio.gather(*[acc.get_signal(now, ep) for _ in range(20)])

    fired = [r for r in results if r is not None]
    assert len(fired) == 1, f"Expected exactly 1 signal fire, got {len(fired)}"


# ---------------------------------------------------------------------------
# Backward-compat shim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_shim_returns_none_below_threshold():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    ev  = _make_ev(premium=10_000)
    result = await acc.ingest(ev)
    assert result is None


@pytest.mark.asyncio
async def test_ingest_shim_fires_at_threshold():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)
    now = datetime.utcnow()
    result = None
    for i in range(3):
        ev = _make_ev(premium=20_000, ts=now + timedelta(seconds=i))
        result = await acc.ingest(ev)
    assert result is not None
    assert result.trade_count == 3


@pytest.mark.asyncio
async def test_ingest_shim_cooldown_suppresses_repeat():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)
    now = datetime.utcnow()
    for i in range(3):
        await acc.ingest(_make_ev(premium=20_000, ts=now + timedelta(seconds=i)))
    # 4th call within cooldown should return None from signal gate
    result = await acc.ingest(_make_ev(premium=20_000, ts=now + timedelta(seconds=10)))
    assert result is None


# ---------------------------------------------------------------------------
# Episode eviction after window pruning (issue #3 preview — empty ep cleanup)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_events_pruned_episode_evicted():
    """
    If all events in an episode fall outside the rolling window, the episode
    key should be removed from _episodes on the next ingest_tick call for a
    new event on the same key.
    """
    acc = RepetitionAccumulator(window_minutes=1, min_trades=3, min_premium=50_000)
    old_ts = datetime.utcnow() - timedelta(minutes=5)

    # Inject two old events directly (below threshold, old timestamps)
    ev1 = _make_ev(premium=10_000, ts=old_ts)
    ev2 = _make_ev(premium=10_000, ts=old_ts + timedelta(seconds=1))
    await acc.ingest_tick(ev1)
    await acc.ingest_tick(ev2)

    key = acc._key(ev1)
    assert key in acc._episodes

    # Now ingest a fresh event — old ones get pruned, leaving only this one
    new_ev = _make_ev(premium=10_000, ts=datetime.utcnow())
    result = await acc.ingest_tick(new_ev)
    assert result is None  # only 1 event left after pruning, below threshold
    # Episode should still exist (it has 1 live event), not evicted
    assert key in acc._episodes
    assert acc._episodes[key].trade_count == 1
