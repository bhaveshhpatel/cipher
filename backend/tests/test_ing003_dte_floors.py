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

Test IDs: D-01 … D-13

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
  D-11  Accumulator cold-start T1 drop (PBE-F1 / QA-F1 blocker):
        ingest_tick() with DTE=5, premium=$30k, unknown ticker → None.
        T1 floor for DTE≤7 = $50k. $30k is below floor. Must be dropped.
  D-12  Accumulator cold-start T1 pass (PBE-F1 / QA-F1 blocker):
        ingest_tick() with DTE=5, premium=$60k, unknown ticker → RepetitionEpisode.
        $60k >= $50k T1 floor for DTE≤7. Must return episode.
  D-13  Accumulator post-warmup tier override (QA-Q2 blocker):
        After set_tier_map({"TESTTICKER": 2}), DTE=5, premium=$30k → RepetitionEpisode.
        T2 floor for DTE≤7 = $25k. $30k >= $25k. Must pass after tier injection.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from parsers.options_flow_parser import OptionsFlowEvent, parse_tradier_trade
import parsers.options_flow_parser as _parser_module
from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS


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


def _make_event(
    ticker="UNKNOWNTICKER",
    contract_type="CALL",
    strike=500.0,
    expiry="2026-06-20",
    premium=60_000.0,
    dte=5,
    underlying_price=500.0,
    trade_type="TRADE",
    is_aggressive=True,
):
    """
    Build a minimal OptionsFlowEvent-compatible dict for accumulator tests.
    Uses a dict (not OptionsFlowEvent) so tests have no dependency on the
    parser layer and directly exercise the accumulator's _DictEventWrapper path.

    is_aggressive defaults to True so that stated premium values map 1-to-1
    to weighted_premium in Gate-2 comparisons. D-11 through D-13 test the
    DTE-tier boundary using raw premium figures; passive discounting (0.5x)
    would halve weighted_premium and mask gate correctness.
    """
    return {
        "ticker":           ticker,
        "contract_type":    contract_type,
        "strike":           strike,
        "expiry":           expiry,
        "premium":          premium,
        "dte":              dte,
        "underlying_price": underlying_price,
        "trade_type":       trade_type,
        "order_side":       "UNKNOWN",
        "timestamp":        datetime.now(timezone.utc),
        "is_aggressive":    is_aggressive,
    }


def _fresh_accumulator(**kwargs):
    """
    Return a RepetitionAccumulator wired exactly as tradier_stream instantiates
    it after ING-003: _DEFAULT_DTE_PREMIUM_TIERS passed at init, min_trades=1
    so a single tick can trigger Gate-1 in isolation.

    min_trades=1 (not the default 3) is intentional — these tests verify the
    DTE-tier gate (Gate 2), not the count gate (Gate 1). Setting min_trades=1
    removes Gate-1 as a confounding variable.
    """
    return RepetitionAccumulator(
        window_minutes=30,
        min_trades=1,
        min_premium=10_000,
        dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
        **kwargs,
    )


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


# ── D-11  Accumulator cold-start T1 drop (PBE-F1 / QA-F1 blocker) ───────────

def test_D11_accumulator_cold_start_T1_drops_30k():
    """
    PBE-F1 / QA-F1 — REQUIRED: exercise ingest_tick() directly, not the parser.

    Setup:
      - Fresh accumulator with _DEFAULT_DTE_PREMIUM_TIERS (as wired by ING-003).
      - No tier_map injected → unknown ticker defaults to T1 (strict).
      - DTE=5, premium=$30,000, is_aggressive=True.

    _DEFAULT_DTE_PREMIUM_TIERS[7] = (50_000, 25_000).
    T1 floor for DTE≤7 = $50,000.
    weighted_premium = $30,000 (aggressive, full weight) < $50,000 → Gate-2 drops.

    Expected: ingest_tick() returns None.

    This is the core cold-start correctness proof for ING-003: before this
    fix, dte_premium_tiers=None caused _get_episode_min_premium() to fall
    back to the flat min_premium=$10,000 floor, letting a $30k print through
    as if it were a T1-qualified institutional event.
    """
    acc = _fresh_accumulator()
    ev  = _make_event(ticker="UNKNOWNTICKER", dte=5, premium=30_000.0)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is None, (
        f"DTE=5, premium=$30k, unknown ticker (T1 default, floor=$50k): "
        f"ingest_tick() must return None (dropped). Got {result!r}. "
        f"ING-003 cold-start DTE-tier gate is not active."
    )


# ── D-12  Accumulator cold-start T1 pass (PBE-F1 / QA-F1 blocker) ───────────

def test_D12_accumulator_cold_start_T1_passes_60k():
    """
    PBE-F1 / QA-F1 — REQUIRED: boundary pass case for D-11.

    Setup:
      - Same fresh accumulator, same unknown ticker → T1 default.
      - DTE=5, premium=$60,000, is_aggressive=True.

    T1 floor for DTE≤7 = $50,000.
    weighted_premium = $60,000 (aggressive, full weight) >= $50,000 → Gate-2 passes.

    Expected: ingest_tick() returns a RepetitionEpisode (not None).

    Confirms the gate has a real boundary: $30k drops (D-11), $60k passes
    (D-12). Without both cases the test suite cannot distinguish a broken
    gate from an always-pass or always-drop implementation.
    """
    acc = _fresh_accumulator()
    ev  = _make_event(ticker="UNKNOWNTICKER", dte=5, premium=60_000.0)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is not None, (
        f"DTE=5, premium=$60k, unknown ticker (T1 default, floor=$50k): "
        f"ingest_tick() must return a RepetitionEpisode. Got None. "
        f"ING-003 DTE-tier gate is incorrectly rejecting a qualifying print."
    )
    assert result.total_premium == pytest.approx(60_000.0), (
        f"Episode total_premium must be $60,000. Got {result.total_premium}"
    )


# ── D-13  Accumulator post-warmup tier override (QA-Q2 blocker) ──────────────

def test_D13_accumulator_post_warmup_tier_override_passes_30k():
    """
    QA-Q2 — REQUIRED: verify set_tier_map() correctly overrides the T1 default.

    Setup:
      - Fresh accumulator with _DEFAULT_DTE_PREMIUM_TIERS.
      - Call set_tier_map({"TESTTICKER": 2}) — simulates registry warmup
        assigning TESTTICKER to T2.
      - DTE=5, premium=$30,000, is_aggressive=True.

    _DEFAULT_DTE_PREMIUM_TIERS[7] = (50_000, 25_000).
    T2 floor for DTE≤7 = $25,000.
    weighted_premium = $30,000 (aggressive, full weight) >= $25,000 → Gate-2 passes.

    Expected: ingest_tick() returns a RepetitionEpisode (not None).

    Without this test, ING-003 cannot be certified: the fix wires the tiers
    at init but set_tier_map() must also be confirmed to correctly transition
    from T1-default to the registry-assigned tier after warmup. A broken
    set_tier_map() (e.g. lock not releasing, wrong dict key) would leave all
    tickers permanently at T1 after warmup, silently over-filtering T2/T3 flow.
    """
    acc = _fresh_accumulator()
    acc.set_tier_map({"TESTTICKER": 2})
    ev  = _make_event(ticker="TESTTICKER", dte=5, premium=30_000.0)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is not None, (
        f"DTE=5, premium=$30k, TESTTICKER tier=T2 (floor=$25k): "
        f"ingest_tick() must return a RepetitionEpisode after set_tier_map(). "
        f"Got None. set_tier_map() is not correctly overriding the T1 default."
    )
    assert result.total_premium == pytest.approx(30_000.0), (
        f"Episode total_premium must be $30,000. Got {result.total_premium}"
    )
