"""
tests/test_evict_lookback_result_cache.py

QA-1: correctness tests for _evict_lookback_result_cache() after the PBE-2
fix that changed _lookback_result_cache from dict[str, bool] to
dict[str, tuple[bool, float]] with self-contained TTL eviction.

Test index:
  EV-1  fresh_entry_not_evicted
        An entry written NOW (age 0) must survive a call to
        _evict_lookback_result_cache(now).

  EV-2  stale_entry_evicted
        An entry whose stamped_at is (now - _LBC_TTL_S - 1) seconds ago
        must be removed by _evict_lookback_result_cache(now).

  EV-3  mixed_entries
        With one fresh and one stale entry, only the stale key is removed;
        the fresh key and its value are preserved intact.

  EV-4  false_value_evicted_correctly
        A (False, stale_ts) entry must age out the same as a True entry.
        Regression guard for any future conditional that skips False values.

  EV-5  eviction_is_independent_of_signal_last_emit  [PBE-2 regression guard]
        A fresh entry whose emit_key is ABSENT from _signal_last_emit must NOT
        be evicted. Pre-PBE-2 code deleted any key missing from _signal_last_emit,
        leaking memory for contracts that never crossed the signal gate.
"""
import time

import pytest

import services.tradier_stream as ts
from services.tradier_stream import _LBC_TTL_S


def _stamp(age_seconds: float = 0.0) -> float:
    """Return a stamped_at value that is `age_seconds` old relative to now."""
    return time.time() - age_seconds


def _setup(entries: dict) -> None:
    """Reset module-level caches and populate _lookback_result_cache."""
    ts._lookback_result_cache.clear()
    ts._signal_last_emit.clear()
    ts._lookback_result_cache.update(entries)


# ---------------------------------------------------------------------------
# EV-1: fresh entry not evicted
# ---------------------------------------------------------------------------

def test_ev1_fresh_entry_not_evicted():
    """
    EV-1: An entry stamped at time.time() (age ~0s) must not be evicted.
    """
    _setup({"AAPL|CALL|150.0|2026-06-20": (True, _stamp(0))})
    ts._evict_lookback_result_cache(time.time())
    assert "AAPL|CALL|150.0|2026-06-20" in ts._lookback_result_cache, (
        "EV-1: Fresh entry must not be evicted. "
        "Check _LBC_TTL_S threshold in _evict_lookback_result_cache."
    )


# ---------------------------------------------------------------------------
# EV-2: stale entry evicted
# ---------------------------------------------------------------------------

def test_ev2_stale_entry_evicted():
    """
    EV-2: An entry older than _LBC_TTL_S + 1 second must be evicted.
    """
    stale_ts = _stamp(_LBC_TTL_S + 1)
    _setup({"TSLA|PUT|200.0|2026-07-18": (False, stale_ts)})
    ts._evict_lookback_result_cache(time.time())
    assert "TSLA|PUT|200.0|2026-07-18" not in ts._lookback_result_cache, (
        f"EV-2: Entry with age > _LBC_TTL_S ({_LBC_TTL_S}s) must be evicted."
    )


# ---------------------------------------------------------------------------
# EV-3: mixed entries — only stale removed
# ---------------------------------------------------------------------------

def test_ev3_mixed_only_stale_removed():
    """
    EV-3: With one fresh and one stale entry, only the stale key is evicted;
    the fresh entry and its value survive unchanged.
    """
    fresh_key = "NVDA|CALL|500.0|2026-09-19"
    stale_key = "SPY|PUT|450.0|2026-08-15"
    fresh_val = (True, _stamp(0))
    stale_val = (True, _stamp(_LBC_TTL_S + 60))

    _setup({fresh_key: fresh_val, stale_key: stale_val})
    ts._evict_lookback_result_cache(time.time())

    assert fresh_key in ts._lookback_result_cache, (
        "EV-3: Fresh entry must survive eviction."
    )
    assert ts._lookback_result_cache[fresh_key] == fresh_val, (
        "EV-3: Fresh entry value must be unchanged after eviction pass."
    )
    assert stale_key not in ts._lookback_result_cache, (
        "EV-3: Stale entry must be removed."
    )


# ---------------------------------------------------------------------------
# EV-4: False value evicted correctly
# ---------------------------------------------------------------------------

def test_ev4_false_value_evicted_correctly():
    """
    EV-4: A (False, stale_ts) entry must age out identically to a True entry.
    Regression guard against conditional logic that might skip False values.
    """
    key = "QQQ|PUT|380.0|2026-06-20"
    _setup({key: (False, _stamp(_LBC_TTL_S + 1))})
    ts._evict_lookback_result_cache(time.time())
    assert key not in ts._lookback_result_cache, (
        "EV-4: (False, stale_ts) entry must be evicted the same as True entries."
    )


# ---------------------------------------------------------------------------
# EV-5: eviction is independent of _signal_last_emit  [PBE-2 regression guard]
# ---------------------------------------------------------------------------

def test_ev5_eviction_independent_of_signal_last_emit():
    """
    EV-5: A fresh _lookback_result_cache entry whose emit_key is ABSENT from
    _signal_last_emit must NOT be evicted.

    Pre-PBE-2 behaviour: _evict_lookback_result_cache() deleted any key not
    present in _signal_last_emit. Contracts that never crossed the signal gate
    (and therefore never wrote to _signal_last_emit) had their cache entries
    continuously purged, leaking the DB fetch cost on every tick.

    Post-PBE-2: eviction is driven solely by stamped_at age vs _LBC_TTL_S.
    Absence from _signal_last_emit is irrelevant.
    """
    key = "MSFT|CALL|400.0|2026-09-19"
    # Populate _lookback_result_cache with a fresh entry.
    _setup({key: (True, _stamp(0))})
    # Explicitly confirm the key is NOT in _signal_last_emit.
    assert key not in ts._signal_last_emit, (
        "Test setup error: emit_key must be absent from _signal_last_emit."
    )

    ts._evict_lookback_result_cache(time.time())

    assert key in ts._lookback_result_cache, (
        "EV-5 [PBE-2 regression]: Fresh entry absent from _signal_last_emit "
        "must NOT be evicted. _evict_lookback_result_cache() must use "
        "stamped_at TTL only, not _signal_last_emit membership."
    )
