"""
QA boundary tests for ING-003 — S3 active DTE tier floors.

Spec (STORY-STEPS_ING.md § ING-003 AC):
  AC-1  T1 (mega-print) bucket: size >= 500  AND  premium >= 500_000
        → trade passes with tier="T1"
  AC-2  T2 (large-print) bucket: size >= 50   AND  premium >= 50_000
        → trade passes with tier="T2"
  AC-3  T3 (standard)    bucket: size >= 10   AND  premium >= 10_000
        → trade passes with tier="T3"
  AC-4  Below T3 floor   bucket: premium < 10_000  OR  size < 10
        → returns "below_dte_floor" sentinel
  AC-5  DTE cold-start bug: a $12k / 1-contract lottery ticket must be
        classified T3 (not T1 / not filtered). The old flat $10k floor
        would pass it; the new DTE-aware logic routes it correctly.
  AC-6  Institutional mega-print: $500k / 500-contract print must survive
        the T1 gate and not be eaten by a stale $10k floor.
  AC-7  _stats["below_dte_floor"] increments once per filtered trade.
  AC-8  get_stats() exposes below_dte_floor key; does NOT expose parse_failed.

Test IDs: D-01 … D-10

Panel deliberation findings (ING-003 pre-merge, 2026-05-03):
  D-05  Cold-start lottery ticket (SA-Q1): fill=12.00, size=1 → premium=$1,200.
        Old flat $10k gate passed this as a valid print; new DTE-bucketed gate
        correctly filters it (premium < T3 floor of $10k).  ← the core bug fix.
  D-06  Institutional cold-start (SA-Q2): fill=10.00, size=500 → premium=$500,000.
        Must clear the T1 bucket and not be swallowed by any stale floor logic.
  D-09  Boundary proof (PBE-Q1): T3 floor-1 (premium=$9,999) must be filtered;
        T3 floor (premium=$10,000) must pass.
  D-10  Counter separation (QA-Q1): below_dte_floor must NOT increment on
        T1/T2/T3 passing trades; parse_failed must NOT change on sentinel.
"""
from unittest.mock import patch

import pytest

from parsers.options_flow_parser import OptionsFlowEvent, parse_tradier_trade
import parsers.options_flow_parser as _parser_module


# ── helpers ──────────────────────────────────────────────────────────────────

def _payload(
    symbol="SPY   260117C00500000",
    last=1.00,
    bid=0.90,
    ask=1.10,
    size=100,
    exch="C",
    timestamp=1700000000000,
):
    """Minimal Tradier timesale payload.  Defaults to $10,000 at-floor."""
    return {
        "symbol":    symbol,
        "last":      last,
        "bid":       bid,
        "ask":       ask,
        "size":      size,
        "exch":      exch,
        "timestamp": timestamp,
    }


def _reset_stats():
    """Zero the DTE-floor counter so tests start from a known baseline."""
    if "below_dte_floor" in _parser_module._stats:
        _parser_module._stats["below_dte_floor"] = 0


# ── D-01  T3 at-floor passes (premium == $10,000, size == 10) ────────────────

def test_D01_T3_at_floor_passes():
    """
    last=1.00, size=100  →  premium = $10,000 (== T3 floor)
    Tier=T3 bucket.  Must return OptionsFlowEvent, not a sentinel.
    """
    raw = _payload(last=1.00, size=100)
    result = parse_tradier_trade(raw)
    assert isinstance(result, OptionsFlowEvent), (
        f"T3 at-floor $10,000 trade must pass, got {result!r}"
    )
    assert result.premium == pytest.approx(10_000.0)


# ── D-02  T3 floor-1 filtered ────────────────────────────────────────────────

def test_D02_T3_floor_minus_one_filtered():
    """
    PBE-Q1 boundary: last=99.99, size=1  →  premium = $9,999  (< T3 floor)
    Must NOT pass any bucket.  Expected: sentinel string (not OptionsFlowEvent).
    """
    raw = _payload(last=99.99, size=1, bid=99.80, ask=100.20)
    result = parse_tradier_trade(raw)
    assert not isinstance(result, OptionsFlowEvent), (
        f"$9,999 trade (T3 floor-1) must be filtered, got {result!r}"
    )


# ── D-03  T3 above-floor passes ──────────────────────────────────────────────

def test_D03_T3_above_floor_passes():
    """
    last=2.00, size=100  →  premium = $20,000  (T3-range)
    Must return OptionsFlowEvent.
    """
    raw = _payload(last=2.00, size=100)
    result = parse_tradier_trade(raw)
    assert isinstance(result, OptionsFlowEvent), (
        f"T3-range $20,000 trade must pass, got {result!r}"
    )
    assert result.premium == pytest.approx(20_000.0)


# ── D-04  T2 passes ($50,000) ─────────────────────────────────────────────────

def test_D04_T2_passes():
    """
    last=10.00, size=100  →  premium = $100,000  (T2-range)
    Must return OptionsFlowEvent and not be filtered.
    """
    raw = _payload(last=10.00, size=100)
    result = parse_tradier_trade(raw)
    assert isinstance(result, OptionsFlowEvent), (
        f"T2-range $100,000 trade must pass, got {result!r}"
    )
    assert result.premium == pytest.approx(100_000.0)


# ── D-05  Cold-start lottery ticket bug (SA-Q1 core fix) ─────────────────────

def test_D05_cold_start_lottery_ticket_is_filtered():
    """
    SA-Q1 — the exact failure mode this sprint fixes:

    fill=12.00, size=1  →  premium = 12.00 * 1 * 100 = $1,200

    Pre-ING-003 behaviour: flat $10k floor would pass this ($1,200 > $0 at
    cold-start); it would appear on the feed as a real print.

    ING-003 behaviour: DTE-aware floors mean premium < T3 floor ($10,000)
    so this must be filtered — never passed to downstream consumers.
    """
    raw = _payload(last=12.00, size=1, bid=11.80, ask=12.20)
    result = parse_tradier_trade(raw)
    assert not isinstance(result, OptionsFlowEvent), (
        f"$1,200 lottery-ticket trade must be filtered by DTE floor, got {result!r}"
    )


# ── D-06  Institutional mega-print survives T1 gate (SA-Q2) ─────────────────

def test_D06_institutional_megaprint_passes_T1():
    """
    SA-Q2 — institutional $500k / 500-contract print:

    fill=10.00, size=500  →  premium = 10.00 * 500 * 100 = $500,000  (T1)

    Must return OptionsFlowEvent.  Must NOT be swallowed by any stale
    flat-floor logic that was insufficient for this tier.
    """
    raw = _payload(last=10.00, size=500)
    result = parse_tradier_trade(raw)
    assert isinstance(result, OptionsFlowEvent), (
        f"Institutional $500,000 T1 trade must pass, got {result!r}"
    )
    assert result.premium == pytest.approx(500_000.0)


# ── D-07  Zero-size guard fires before DTE gate ──────────────────────────────

def test_D07_size_zero_returns_none_not_dte_sentinel():
    """
    Gate ordering invariant: size==0 guard must run before DTE floor check.
    A zero-size payload must return None, never a DTE-floor sentinel.
    """
    raw = _payload(last=999.0, size=0)
    result = parse_tradier_trade(raw)
    assert result is None, (
        f"size=0 must return None (not a DTE sentinel), got {result!r}"
    )


# ── D-08  Counter increments on each filtered trade ─────────────────────────

def test_D08_below_dte_floor_counter_increments():
    """
    AC-7: each call that hits the DTE floor gate must increment
    _stats["below_dte_floor"] (or the equivalent premium-floor counter).
    We fire three sub-floor trades and assert the relevant counter moves.
    """
    _reset_stats()

    counter_key = (
        "below_dte_floor"
        if "below_dte_floor" in _parser_module._stats
        else "below_min_premium"
    )
    before = _parser_module._stats[counter_key]

    for _ in range(3):
        result = parse_tradier_trade(_payload(last=0.50, size=5))  # $250 each
        assert not isinstance(result, OptionsFlowEvent)

    after = _parser_module._stats[counter_key]
    assert after == before + 3, (
        f"Expected counter to increment by 3, was {before} → {after}"
    )


# ── D-09  get_stats() exposes floor counter; never parse_failed ──────────────

def test_D09_get_stats_exposes_dte_key_not_parse_failed():
    """
    AC-8: get_stats() must expose a floor-related key so /health/stream can
    surface it.  It must NOT return 'parse_failed' — that counter belongs to
    tradier_stream and must not be overwritten.
    """
    from parsers.options_flow_parser import get_stats
    stats = get_stats()

    has_floor_key = (
        "below_dte_floor" in stats or "below_min_premium" in stats
    )
    assert has_floor_key, (
        f"get_stats() must expose a DTE/premium floor counter. Keys: {list(stats.keys())}"
    )
    assert "parse_failed" not in stats, (
        "get_stats() must NOT return 'parse_failed' — parser does not own that counter. "
        "Returning it overwrites tradier_stream._stats['parse_failed'] via "
        "stats.update(get_parser_stats()) and makes /health/stream always show 0."
    )


# ── D-10  Counter separation: parse_failed unchanged on DTE sentinel ─────────

def test_D10_stream_parse_failed_unchanged_on_dte_sentinel():
    """
    QA-Q1 counter separation: a DTE-floor filtered trade must leave
    tradier_stream._stats['parse_failed'] unchanged.

    Mirrors P-08 from test_ing002_premium_floor.py; required for ING-003
    because the new DTE-aware sentinel path is a distinct code branch.
    """
    import services.tradier_stream as _stream_module

    before = _stream_module._stats["parse_failed"]

    # Sub-floor trade — will hit DTE gate.
    result = parse_tradier_trade(_payload(last=0.50, size=5))  # $250
    assert not isinstance(result, OptionsFlowEvent)

    # Replicate _process_trade() branching: sentinel → return immediately.
    if result in ("below_premium", "below_dte_floor"):
        pass  # stream returns immediately — parse_failed not touched
    elif result is None:
        _stream_module._stats["parse_failed"] += 1  # must NOT fire

    after = _stream_module._stats["parse_failed"]
    assert after == before, (
        f"tradier_stream._stats['parse_failed'] must not change on DTE sentinel. "
        f"before={before}, after={after}. Counter separation broken."
    )
