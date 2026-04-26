"""
Coverage boost for utils/dedup.py.

Covers missing lines:
  - make_key() multi-arg form
  - make_key() single-arg form (event object)
  - DedupCache.evict_expired()
  - DedupCache.mark_seen()
  - DedupCache.clear()
  - DedupCache.get_exchange_count()
  - DedupCache.is_sweep() True and False
  - DedupCache.dedup_stats()
  - DedupCache._cleanup() triggered after 10s
  - DedupCache.is_duplicate() multi-arg form with exchange
  - DedupCache._cache property setter
  - DedupCache.ttl_seconds property
  - flow_dedup singleton exists
"""
import time
from types import SimpleNamespace

import pytest

from utils.dedup import DedupCache, make_key, flow_dedup


# --- make_key ---

def test_make_key_multi_arg():
    k = make_key("AAPL231215C00180000", 10, 3.50)
    assert k == "AAPL231215C00180000|10|3.50"

def test_make_key_single_arg_event():
    ev = SimpleNamespace(ticker="AAPL", expiry="2026-06-20",
                         contract_type="CALL", strike=180.0)
    k = make_key(ev)
    assert "AAPL" in k
    assert "180.00" in k

def test_make_key_event_occ_symbol_fallback():
    ev = SimpleNamespace(occ_symbol="AAPL231215C00180000",
                         expiry="2026-06-20", contract_type="CALL", strike=180.0)
    k = make_key(ev)
    assert "AAPL231215C00180000" in k


# --- DedupCache basic operations ---

def test_dedup_cache_ttl_property():
    c = DedupCache(ttl_seconds=7.0)
    assert c.ttl_seconds == pytest.approx(7.0)

def test_dedup_cache_mark_seen():
    c = DedupCache(ttl_seconds=5.0)
    c.mark_seen("some_key")
    assert "some_key" in c._cache

def test_dedup_cache_clear():
    c = DedupCache(ttl_seconds=5.0)
    c.mark_seen("k1")
    c.mark_seen("k2")
    c.clear()
    assert c.size() == 0

def test_dedup_cache_setter():
    c = DedupCache(ttl_seconds=5.0)
    c._cache = {"x": time.time()}
    assert "x" in c._seen


# --- evict_expired ---

def test_evict_expired_removes_old_entries():
    c = DedupCache(ttl_seconds=5.0)
    c._seen["old_key"] = time.time() - 10.0  # expired
    c._seen["new_key"] = time.time()          # fresh
    evicted = c.evict_expired()
    assert evicted == 1
    assert "old_key" not in c._seen
    assert "new_key" in c._seen

def test_evict_expired_nothing_to_evict():
    c = DedupCache(ttl_seconds=5.0)
    c.mark_seen("fresh")
    assert c.evict_expired() == 0


# --- is_duplicate: multi-arg form ---

def test_is_duplicate_multi_arg_first_is_not_dup():
    c = DedupCache(ttl_seconds=5.0)
    assert c.is_duplicate("AAPL", 10, 3.5, exchange="CBOE") is False

def test_is_duplicate_multi_arg_second_is_dup():
    c = DedupCache(ttl_seconds=5.0)
    c.is_duplicate("AAPL", 10, 3.5, exchange="CBOE")
    assert c.is_duplicate("AAPL", 10, 3.5, exchange="MIAX") is True

def test_is_duplicate_multi_arg_expired_is_not_dup():
    c = DedupCache(ttl_seconds=1.0)
    key = make_key("AAPL", 10, 3.5)
    c._seen[key] = time.time() - 5.0  # already expired
    assert c.is_duplicate("AAPL", 10, 3.5, exchange="CBOE") is False


# --- get_exchange_count / is_sweep ---

def test_get_exchange_count_multiple_exchanges():
    c = DedupCache(ttl_seconds=5.0, sweep_window=10.0, sweep_min_exchanges=3)
    c.is_duplicate("SYM", 5, 1.0, exchange="CBOE")
    c.is_duplicate("SYM", 5, 1.0, exchange="MIAX")
    c.is_duplicate("SYM", 5, 1.0, exchange="PHLX")
    assert c.get_exchange_count("SYM", 5, 1.0) == 3

def test_is_sweep_true():
    c = DedupCache(ttl_seconds=5.0, sweep_window=10.0, sweep_min_exchanges=3)
    c.is_duplicate("SYM", 5, 1.0, exchange="CBOE")
    c.is_duplicate("SYM", 5, 1.0, exchange="MIAX")
    c.is_duplicate("SYM", 5, 1.0, exchange="PHLX")
    assert c.is_sweep("SYM", 5, 1.0) is True

def test_is_sweep_false_too_few_exchanges():
    c = DedupCache(ttl_seconds=5.0, sweep_window=10.0, sweep_min_exchanges=3)
    c.is_duplicate("SYM", 5, 1.0, exchange="CBOE")
    c.is_duplicate("SYM", 5, 1.0, exchange="MIAX")
    assert c.is_sweep("SYM", 5, 1.0) is False

def test_is_sweep_increments_counter():
    c = DedupCache(ttl_seconds=5.0, sweep_window=10.0, sweep_min_exchanges=2)
    c.is_duplicate("SYM", 5, 1.0, exchange="A")
    c.is_duplicate("SYM", 5, 1.0, exchange="B")
    c.is_sweep("SYM", 5, 1.0)
    assert c.dedup_stats()["dedup_sweeps"] == 1


# --- dedup_stats ---

def test_dedup_stats_initial():
    c = DedupCache(ttl_seconds=5.0)
    stats = c.dedup_stats()
    assert stats["dedup_seen"]       == 0
    assert stats["dedup_duplicates"] == 0
    assert stats["dedup_sweeps"]     == 0
    assert stats["dedup_cache_size"] == 0

def test_dedup_stats_after_events():
    c = DedupCache(ttl_seconds=5.0)
    c.is_duplicate("K1", 1, 1.0)
    c.is_duplicate("K1", 1, 1.0)  # dup
    c.is_duplicate("K2", 1, 1.0)
    stats = c.dedup_stats()
    assert stats["dedup_seen"]       == 2
    assert stats["dedup_duplicates"] == 1
    assert stats["dedup_cache_size"] == 2


# --- flow_dedup singleton ---

def test_flow_dedup_singleton_exists():
    assert flow_dedup is not None
    assert flow_dedup.ttl_seconds == pytest.approx(5.0)
