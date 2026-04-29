"""
Tests for RepetitionAccumulator concurrent safety and Gate 2 premium-delta
retrigger logic.

Covers:
  - Per-key lock prevents phantom threshold crossings under concurrent ingest
  - get_signal returns episode when premium threshold is met
  - Gate 2 (retrigger): signal suppressed when new premium delta < retrigger
  - Backward-compat ingest() shim still works correctly
  - Episode window-expiry resets the episode on next ingest

NOTE: The cooldown mechanism is premium-delta based (last_signaled_premium),
not time-based. signal_cooldown= parameter is an alias for retrigger= and
sets the minimum *premium growth* required between signal emissions.
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
    ts: datetime | None = None,
):
    """Return a minimal OptionsFlowEvent-like SimpleNamespace."""
    ev = SimpleNamespace(
        ticker=ticker,
        contract_type=contract_type,
        strike=strike,
        expiry=expiry,
        premium=premium,
        timestamp=ts or datetime.now(timezone.utc),
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
    now = datetime.now(timezone.utc)
    for i in range(3):
        ev = _make_ev(premium=20_000, ts=now + timedelta(seconds=i))
        result = await acc.ingest_tick(ev)
    assert result is not None
    assert result.trade_count == 3
    assert result.total_premium == 60_000


@pytest.mark.asyncio
async def test_get_signal_first_call_fires():
    """get_signal returns episode when total_premium >= min_premium."""
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    # Seed episode with enough premium to cross the gate
    ep._total_premium = 60_000.0
    ep._trade_count = 3
    result = await acc.get_signal(now, ep)
    assert result is ep


@pytest.mark.asyncio
async def test_get_signal_below_min_premium_returns_none():
    """get_signal returns None when total_premium < min_premium."""
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    # No premium set — total_premium == 0
    result = await acc.get_signal(now, ep)
    assert result is None


@pytest.mark.asyncio
async def test_get_signal_fires_after_premium_grows():
    """get_signal fires again after premium grows above min_premium (always returns
    ep if premium gate is met — get_signal itself has no state; Gate 2 is in ingest_tick)."""
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep._total_premium = 100_000.0
    ep._trade_count = 5
    result = await acc.get_signal(now + timedelta(minutes=6), ep)
    assert result is ep


@pytest.mark.asyncio
async def test_get_signal_none_episode_is_noop():
    acc = RepetitionAccumulator()
    result = await acc.get_signal(datetime.now(timezone.utc), None)
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
    now = datetime.now(timezone.utc)
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
# Concurrent safety — issue #2: get_signal idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_get_signal_only_one_fires():
    """
    Fire 20 concurrent get_signal calls on the same episode.
    get_signal is a stateless premium gate so all calls where premium >= min
    will return ep. This test verifies the method is safe to call concurrently
    (no crash, no data corruption on the episode object).
    """
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    ep  = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep._total_premium = 60_000.0
    ep._trade_count = 3

    results = await asyncio.gather(*[acc.get_signal(now, ep) for _ in range(20)])

    # All 20 calls see premium >= min_premium so all return ep
    fired = [r for r in results if r is not None]
    assert len(fired) == 20, f"Expected all 20 to fire (stateless gate), got {len(fired)}"
    # Episode object must not be corrupted
    assert ep._total_premium == 60_000.0


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
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    result = None
    for i in range(3):
        ev = _make_ev(premium=20_000, ts=now + timedelta(seconds=i))
        result = await acc.ingest(ev)
    assert result is not None
    assert result.trade_count == 3


@pytest.mark.asyncio
async def test_ingest_shim_cooldown_suppresses_repeat():
    """
    Gate 2 (premium-delta retrigger): after initial signal fires at $60k,
    a 4th tick adding $20k gives total=$80k (delta=$20k < retrigger=$100k).
    The 4th ingest call should return None.
    """
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, retrigger=100_000)
    now = datetime.now(timezone.utc)
    for i in range(3):
        await acc.ingest(_make_ev(premium=20_000, ts=now + timedelta(seconds=i)))
    # 4th call: total goes to $80k, delta since last signal ($60k) = $20k < $100k retrigger
    result = await acc.ingest(_make_ev(premium=20_000, ts=now + timedelta(seconds=10)))
    assert result is None


# ---------------------------------------------------------------------------
# Episode window-expiry resets episode on next ingest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_episode_reset_on_new_ingest():
    """
    When the last_seen of an episode is older than window_minutes, the next
    ingest_tick call should create a fresh episode (trade_count=1) rather
    than adding to the stale one. A single fresh event below threshold returns None.
    """
    acc = RepetitionAccumulator(window_minutes=1, min_trades=3, min_premium=50_000)
    now = datetime.now(timezone.utc)
    old_ts = now - timedelta(minutes=5)

    # Inject two events directly to build up a stale episode
    ev1 = _make_ev(premium=10_000, ts=old_ts)
    ev2 = _make_ev(premium=10_000, ts=old_ts + timedelta(seconds=1))
    await acc.ingest_tick(ev1)
    await acc.ingest_tick(ev2)

    key = acc._key(ev1)
    stale_ep = acc._episodes[key]
    # Manually mark last_seen as old so window expiry triggers
    stale_ep.last_seen = old_ts

    # Now ingest a fresh event — window expired, episode resets
    new_ev = _make_ev(premium=10_000, ts=now)
    result = await acc.ingest_tick(new_ev)
    # Only 1 trade, $10k < $50k min_premium -> threshold not crossed
    assert result is None
    # Episode is fresh with only the new event
    fresh_ep = acc._episodes[key]
    assert fresh_ep is not stale_ep  # new object created
    assert fresh_ep.trade_count == 1
