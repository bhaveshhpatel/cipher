"""
Unit tests for C-019 Layer 4 DedupCache (utils/dedup.py).

Covers:
  1.  First print is NOT a duplicate (canonical)
  2.  Second print of same trade within TTL IS a duplicate
  3.  Same trade after TTL expires is treated as a new canonical
  4.  Different size is NOT a duplicate
  5.  Different fill (> 0.1 delta) is NOT a duplicate
  6.  Fill rounding: fills differing by \u00b10.01 are treated as same trade
  7.  Different occ_symbol is NOT a duplicate
  8.  _dedup_key is deterministic
  9.  dedup_stats() starts at zero
  10. dedup_stats() increments total_seen correctly
  11. dedup_stats() increments total_duplicates correctly
  12. is_sweep() returns False when < 3 distinct exchanges
  13. is_sweep() returns True when 3+ distinct exchanges within window
  14. get_exchange_count() counts unique exchanges only
  15. Empty exchange string is excluded from sweep count
  16. is_sweep() returns False after sweep window expires
  17. dedup_stats() increments total_sweeps on confirmed sweep
  18. dedup_cache_size reflects internal seen dict length
  19. Lazy cleanup removes expired entries after 10s
  20. Module-level singleton flow_dedup is a DedupCache instance
  21. _process_trade drops duplicate + increments _stats[\"deduped\"]
  22. _process_trade upgrades trade_type to SWEEP on multi-exchange print
"""
import time
import asyncio
import unittest.mock as mock
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# Ensure backend/ is on sys.path (conftest.py handles this)
from utils.dedup import DedupCache, flow_dedup


# ── helpers ─────────────────────────────────────────────────────────────────
_OCC  = "AAPL  260117C00180000"
_SIZE = 100
_FILL = 3.45
_EXCH = "C"   # CBOE


def fresh() -> DedupCache:
    """Return a new DedupCache with default parameters."""
    return DedupCache(ttl_seconds=5.0, sweep_window=8.0, sweep_min_exchanges=3)


def mono() -> float:
    return time.monotonic()


# ── 1: canonical print not a duplicate ────────────────────────────────────────
def test_first_print_not_duplicate():
    c = fresh()
    assert c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=mono()) is False


# ── 2: second print within TTL is duplicate ─────────────────────────────────
def test_second_print_within_ttl_is_duplicate():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    assert c.is_duplicate(_OCC, _SIZE, _FILL, "M", ts=t0 + 1.0) is True


# ── 3: same trade after TTL is new canonical ────────────────────────────────
def test_same_trade_after_ttl_is_new_canonical():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    # 6 seconds later — past the 5s TTL
    assert c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0 + 6.0) is False


# ── 4: different size → not a duplicate ─────────────────────────────────────
def test_different_size_not_duplicate():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    assert c.is_duplicate(_OCC, _SIZE + 1, _FILL, "M", ts=t0 + 0.5) is False


# ── 5: different fill (> 0.1 delta) → not duplicate ─────────────────────────
def test_different_fill_not_duplicate():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    # 3.55 rounds to 3.6 vs 3.45 rounds to 3.5 → different keys
    assert c.is_duplicate(_OCC, _SIZE, _FILL + 0.15, "M", ts=t0 + 0.5) is False


# ── 6: fill rounding absorbs ±$0.01 ─────────────────────────────────────────
def test_fill_rounding_absorbs_penny_difference():
    c = fresh()
    t0 = mono()
    # 3.45 and 3.44 both round to 3.4 at 1dp → same dedup key
    c.is_duplicate(_OCC, _SIZE, 3.45, _EXCH, ts=t0)
    assert c.is_duplicate(_OCC, _SIZE, 3.44, "M", ts=t0 + 0.5) is True


# ── 7: different occ_symbol → not duplicate ────────────────────────────────
def test_different_occ_not_duplicate():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    other_occ = "TSLA  260117C00250000"
    assert c.is_duplicate(other_occ, _SIZE, _FILL, "M", ts=t0 + 0.5) is False


# ── 8: _dedup_key is deterministic ──────────────────────────────────────────
def test_dedup_key_deterministic():
    k1 = DedupCache._dedup_key(_OCC, _SIZE, _FILL)
    k2 = DedupCache._dedup_key(_OCC, _SIZE, _FILL)
    assert k1 == k2
    assert "|" in k1


# ── 9: dedup_stats starts at zero ──────────────────────────────────────────
def test_dedup_stats_initial_zero():
    c = fresh()
    s = c.dedup_stats()
    assert s["dedup_seen"]       == 0
    assert s["dedup_duplicates"] == 0
    assert s["dedup_sweeps"]     == 0
    assert s["dedup_cache_size"] == 0


# ── 10: dedup_stats total_seen increments on canonical ────────────────────────
def test_dedup_stats_seen_increments():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    c.is_duplicate("TSLA  260117C00250000", _SIZE, _FILL, _EXCH, ts=t0)
    assert c.dedup_stats()["dedup_seen"] == 2


# ── 11: dedup_stats total_duplicates increments on dup ──────────────────────
def test_dedup_stats_duplicates_increments():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "M",   ts=t0 + 1.0)  # dup
    c.is_duplicate(_OCC, _SIZE, _FILL, "X",   ts=t0 + 2.0)  # dup
    assert c.dedup_stats()["dedup_duplicates"] == 2


# ── 12: is_sweep False when < 3 exchanges ───────────────────────────────────
def test_is_sweep_false_below_threshold():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, "C", ts=t0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "M", ts=t0 + 1.0)
    # Only 2 distinct exchanges — NOT a sweep
    assert c.is_sweep(_OCC, _SIZE, _FILL) is False


# ── 13: is_sweep True when 3+ distinct exchanges within window ──────────────
def test_is_sweep_true_three_exchanges():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, "C", ts=t0)       # canonical
    c.is_duplicate(_OCC, _SIZE, _FILL, "M", ts=t0 + 1.0) # dup exch M
    c.is_duplicate(_OCC, _SIZE, _FILL, "X", ts=t0 + 2.0) # dup exch X
    assert c.is_sweep(_OCC, _SIZE, _FILL) is True


# ── 14: get_exchange_count counts unique exchanges ───────────────────────────
def test_get_exchange_count_unique_only():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, "C", ts=t0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "C", ts=t0 + 0.1)  # same exchange twice
    c.is_duplicate(_OCC, _SIZE, _FILL, "M", ts=t0 + 1.0)
    # Unique exchanges: C and M = 2
    assert c.get_exchange_count(_OCC, _SIZE, _FILL) == 2


# ── 15: empty exchange string excluded from sweep count ─────────────────────
def test_empty_exchange_excluded_from_sweep_count():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, "",  ts=t0)        # empty — ignored
    c.is_duplicate(_OCC, _SIZE, _FILL, "M", ts=t0 + 1.0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "X", ts=t0 + 2.0)
    # Only 2 real exchanges (M and X), empty excluded → NOT a sweep
    assert c.is_sweep(_OCC, _SIZE, _FILL) is False


# ── 16: is_sweep False after sweep window expires ───────────────────────────
def test_is_sweep_false_after_window_expires():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, "C", ts=t0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "M", ts=t0 + 1.0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "X", ts=t0 + 2.0)
    # All hits are 9s old (> 8s sweep window) → not a sweep now
    with patch("time.monotonic", return_value=t0 + 9.0):
        count = c.get_exchange_count(_OCC, _SIZE, _FILL)
    assert count == 0


# ── 17: dedup_stats total_sweeps increments on sweep ────────────────────────
def test_dedup_stats_sweeps_increments():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, "C", ts=t0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "M", ts=t0 + 1.0)
    c.is_duplicate(_OCC, _SIZE, _FILL, "X", ts=t0 + 2.0)
    c.is_sweep(_OCC, _SIZE, _FILL)  # triggers sweep count
    assert c.dedup_stats()["dedup_sweeps"] == 1


# ── 18: dedup_cache_size reflects internal _seen dict ───────────────────────
def test_dedup_cache_size():
    c = fresh()
    t0 = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=t0)
    c.is_duplicate("TSLA  260117C00250000", _SIZE, _FILL, _EXCH, ts=t0)
    assert c.dedup_stats()["dedup_cache_size"] == 2


# ── 19: lazy cleanup removes expired entries after 10s ──────────────────────
def test_lazy_cleanup_removes_expired():
    c = fresh()
    base = mono()
    c.is_duplicate(_OCC, _SIZE, _FILL, _EXCH, ts=base)
    # Force last_cleanup to 11s ago so next call triggers cleanup
    c._last_cleanup = base - 11.0
    # All seen entries are older than max(ttl=5, sweep_win=8) = 8s
    # Simulate "now" being 9s after base
    with patch("time.monotonic", return_value=base + 9.0):
        c._cleanup()
    assert c.dedup_stats()["dedup_cache_size"] == 0


# ── 20: module-level singleton is DedupCache ───────────────────────────────
def test_flow_dedup_singleton_is_dedupcache():
    assert isinstance(flow_dedup, DedupCache)
    assert flow_dedup._ttl == 5.0
    assert flow_dedup._sweep_win == 8.0
    assert flow_dedup._sweep_min == 3


# ── 21: _process_trade drops duplicate + increments _stats["deduped"] ────────
def test_process_trade_drops_duplicate_and_increments_stats():
    """
    Regression for C-019: flow_dedup was instantiated but never called.
    Verify _process_trade actually invokes is_duplicate() and drops the event.
    """
    import services.tradier_stream as ts_mod

    ts_mod._stats["deduped"] = 0
    ts_mod._stats["ticks"]   = 0

    fake_dedup = MagicMock(spec=DedupCache)
    fake_dedup.is_duplicate.return_value = True   # force duplicate

    raw = {
        "type":     "timesale",
        "timesale": {
            "symbol": _OCC,
            "last":   _FILL,
            "bid":    3.40,
            "ask":    3.50,
            "size":   _SIZE,
            "exch":   "M",
            "date":   "1700000000000",
        },
    }

    with patch.object(ts_mod, "flow_dedup", fake_dedup), \
         patch("parsers.options_flow_parser.parse_tradier_trade") as mock_parse:

        mock_ev = MagicMock()
        mock_ev.size       = _SIZE
        mock_ev.fill_price = _FILL
        mock_parse.return_value = mock_ev

        asyncio.get_event_loop().run_until_complete(ts_mod._process_trade(raw))

    assert fake_dedup.is_duplicate.called
    assert ts_mod._stats["deduped"] == 1


# ── 22: _process_trade upgrades trade_type to SWEEP ─────────────────────────
def test_process_trade_upgrades_to_sweep():
    import services.tradier_stream as ts_mod

    ts_mod._stats["ticks"]      = 0
    ts_mod._stats["classified"] = 0

    fake_dedup = MagicMock(spec=DedupCache)
    fake_dedup.is_duplicate.return_value    = False  # canonical
    fake_dedup.is_sweep.return_value        = True   # sweep!
    fake_dedup.get_exchange_count.return_value = 4

    raw = {
        "type":     "timesale",
        "timesale": {
            "symbol": _OCC,
            "last":   _FILL,
            "bid":    3.40,
            "ask":    3.50,
            "size":   _SIZE,
            "exch":   "C",
            "date":   "1700000000000",
        },
    }

    captured_ev = {}

    with patch.object(ts_mod, "flow_dedup", fake_dedup), \
         patch("parsers.options_flow_parser.parse_tradier_trade") as mock_parse, \
         patch.object(ts_mod, "persist_flow_event", new_callable=AsyncMock), \
         patch.object(ts_mod, "accumulator") as mock_acc:

        mock_ev = MagicMock()
        mock_ev.size           = _SIZE
        mock_ev.fill_price     = _FILL
        mock_ev.trade_type     = "BLOCK"          # starts as BLOCK
        mock_ev.exchange_count = 1
        mock_parse.return_value = mock_ev
        mock_acc.ingest.return_value = None        # no signal emitted

        asyncio.get_event_loop().run_until_complete(ts_mod._process_trade(raw))
        captured_ev["trade_type"]     = mock_ev.trade_type
        captured_ev["exchange_count"] = mock_ev.exchange_count

    assert captured_ev["trade_type"]     == "SWEEP"
    assert captured_ev["exchange_count"] == 4
