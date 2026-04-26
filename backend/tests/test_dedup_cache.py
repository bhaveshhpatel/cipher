"""
Regression tests for services/dedup_cache.py
"""
from unittest.mock import MagicMock
from services.dedup_cache import DedupCache


def _cache(ttl=5):
    return DedupCache(ttl_seconds=ttl)


def test_empty_cache_not_duplicate():
    c = _cache()
    assert not c.is_duplicate("key1")


def test_seen_key_is_duplicate():
    c = _cache()
    c.mark_seen("key1")
    assert c.is_duplicate("key1")


def test_unseen_key_not_duplicate():
    c = _cache()
    c.mark_seen("key1")
    assert not c.is_duplicate("key2")


def test_mark_seen_returns_none_or_bool():
    c = _cache()
    result = c.mark_seen("key1")
    assert result is None or isinstance(result, bool)


def test_different_keys_independent():
    c = _cache()
    c.mark_seen("a")
    c.mark_seen("b")
    assert c.is_duplicate("a")
    assert c.is_duplicate("b")
    assert not c.is_duplicate("c")


def test_cache_size_grows_with_entries():
    c = _cache()
    for i in range(5):
        c.mark_seen(f"key{i}")
    assert c.size() >= 5


def test_clear_removes_all_entries():
    c = _cache()
    c.mark_seen("key1")
    c.mark_seen("key2")
    c.clear()
    assert not c.is_duplicate("key1")
    assert not c.is_duplicate("key2")


def test_size_zero_after_clear():
    c = _cache()
    c.mark_seen("key1")
    c.clear()
    assert c.size() == 0


def test_is_duplicate_does_not_mutate_cache():
    c = _cache()
    c.is_duplicate("key1")
    assert not c.is_duplicate("key1")  # still not seen


def test_mark_seen_twice_still_duplicate():
    c = _cache()
    c.mark_seen("key1")
    c.mark_seen("key1")
    assert c.is_duplicate("key1")


def test_cache_accepts_occ_symbol_keys():
    c = _cache()
    occ = "AAPL  250117C00180000"
    c.mark_seen(occ)
    assert c.is_duplicate(occ)


def test_cache_key_case_sensitive():
    c = _cache()
    c.mark_seen("AAPL")
    assert not c.is_duplicate("aapl")


def test_mock_integration_with_dedup_cache():
    """DedupCache works as a collaborator dependency in mocked context."""
    mock_processor = MagicMock()
    cache = _cache()

    def process(key):
        if not cache.is_duplicate(key):
            cache.mark_seen(key)
            mock_processor(key)

    process("key1")
    process("key1")  # duplicate — should not call processor again
    process("key2")

    assert mock_processor.call_count == 2
