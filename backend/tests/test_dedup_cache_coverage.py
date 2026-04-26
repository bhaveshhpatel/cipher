"""
Coverage boost for services/dedup_cache.py.
Targets: evict_expired (lines 58-62).
"""
from unittest.mock import patch
import time

from services.dedup_cache import DedupCache


def test_evict_expired_removes_old_keys():
    cache = DedupCache(ttl_seconds=0.01)
    cache.mark_seen("key1")
    cache.mark_seen("key2")
    # Force expiry by backdating timestamps
    for k in list(cache._store):
        cache._store[k] = time.monotonic() - 1.0
    removed = cache.evict_expired()
    assert removed == 2
    assert cache.size() == 0


def test_evict_expired_keeps_fresh_keys():
    cache = DedupCache(ttl_seconds=60)
    cache.mark_seen("fresh")
    removed = cache.evict_expired()
    assert removed == 0
    assert cache.size() == 1


def test_evict_expired_mixed():
    cache = DedupCache(ttl_seconds=60)
    cache.mark_seen("fresh")
    cache.mark_seen("stale")
    cache._store["stale"] = time.monotonic() - 120
    removed = cache.evict_expired()
    assert removed == 1
    assert cache.is_duplicate("fresh")
    assert not cache.is_duplicate("stale")


def test_evict_expired_empty_cache():
    cache = DedupCache()
    assert cache.evict_expired() == 0


def test_clear_resets_store():
    cache = DedupCache()
    cache.mark_seen("a")
    cache.clear()
    assert cache.size() == 0
