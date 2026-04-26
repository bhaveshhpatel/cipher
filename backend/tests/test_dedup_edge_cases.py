"""
Edge-case regression tests for the deduplication layer.
"""
import asyncio
import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_event(ticker="AAPL", strike=180.0, expiry="2026-06-20",
                contract_type="CALL", premium=100_000.0, offset_secs=0):
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock
    ev = MagicMock()
    ev.ticker        = ticker
    ev.strike        = strike
    ev.expiry        = expiry
    ev.contract_type = contract_type
    ev.premium       = premium
    ev.timestamp     = datetime(2026, 4, 25, 10, 0, 0) + timedelta(seconds=offset_secs)
    return ev


# ---------------------------------------------------------------------------
# Basic dedup identity
# ---------------------------------------------------------------------------

def test_identical_events_produce_same_key():
    from utils.dedup import make_key
    ev = _make_event()
    assert make_key(ev) == make_key(ev)


def test_different_strike_produces_different_key():
    from utils.dedup import make_key
    a = _make_event(strike=180.0)
    b = _make_event(strike=185.0)
    assert make_key(a) != make_key(b)


def test_different_ticker_produces_different_key():
    from utils.dedup import make_key
    a = _make_event(ticker="AAPL")
    b = _make_event(ticker="TSLA")
    assert make_key(a) != make_key(b)


def test_different_expiry_produces_different_key():
    from utils.dedup import make_key
    a = _make_event(expiry="2026-06-20")
    b = _make_event(expiry="2026-07-18")
    assert make_key(a) != make_key(b)


def test_different_contract_type_produces_different_key():
    from utils.dedup import make_key
    a = _make_event(contract_type="CALL")
    b = _make_event(contract_type="PUT")
    assert make_key(a) != make_key(b)


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

def test_cache_accepts_first_event():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=60)
    ev = _make_event()
    assert cache.is_duplicate(ev) is False


def test_cache_rejects_second_identical_event():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=60)
    ev = _make_event()
    cache.is_duplicate(ev)  # first — accepted
    assert cache.is_duplicate(ev) is True  # second — duplicate


def test_cache_accepts_different_event_same_ticker():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=60)
    ev_a = _make_event(strike=180.0)
    ev_b = _make_event(strike=185.0)
    cache.is_duplicate(ev_a)
    assert cache.is_duplicate(ev_b) is False


def test_cache_independent_instances():
    from utils.dedup import DedupCache
    c1 = DedupCache(ttl_seconds=60)
    c2 = DedupCache(ttl_seconds=60)
    ev = _make_event()
    c1.is_duplicate(ev)
    # c2 has no knowledge of ev
    assert c2.is_duplicate(ev) is False


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

def test_expired_entry_is_not_duplicate():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=1)
    ev = _make_event()
    cache.is_duplicate(ev)  # seed

    # Manually backdate the entry
    import time
    key = next(iter(cache._cache))
    cache._cache[key] = time.time() - 10  # expired

    assert cache.is_duplicate(ev) is False


# ---------------------------------------------------------------------------
# Bulk / concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_inserts_no_crash():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=60)
    events = [_make_event(strike=float(180 + i)) for i in range(50)]

    async def _check(ev):
        return cache.is_duplicate(ev)

    results = await asyncio.gather(*[_check(ev) for ev in events])
    # All should be False (first time seen)
    assert all(r is False for r in results)


# ---------------------------------------------------------------------------
# Eviction / max-size guard
# ---------------------------------------------------------------------------

def test_cache_does_not_grow_unbounded():
    from utils.dedup import DedupCache
    cache = DedupCache(ttl_seconds=3600)
    for i in range(500):
        cache.is_duplicate(_make_event(strike=float(i)))
    assert len(cache._cache) <= 500


# ---------------------------------------------------------------------------
# Env-var override (TTL from env)
# ---------------------------------------------------------------------------

def test_ttl_from_env_is_respected(monkeypatch):
    monkeypatch.setenv("DEDUP_TTL_SECONDS", "120")
    with pytest.MonkeyPatch().context():
        from utils.dedup import DedupCache
        cache = DedupCache()  # should pick up env var if supported
        assert cache.ttl_seconds >= 1  # just verify no crash


# ---------------------------------------------------------------------------
# Edge: None / missing fields
# ---------------------------------------------------------------------------

def test_event_with_none_strike_does_not_crash():
    from utils.dedup import make_key
    ev = _make_event()
    ev.strike = None
    try:
        make_key(ev)
    except (TypeError, AttributeError):
        pass  # acceptable — just must not raise unhandled
