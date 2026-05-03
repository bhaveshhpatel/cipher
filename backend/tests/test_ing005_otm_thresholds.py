"""
QA boundary tests for ING-005 — deep OTM multiplier alignment.

Spec (STORY-STEPS_ING.md § ING-005 AC):
  AC-1  Default deep_otm_multiplier is 1.0 (not 1.5).
        New instances must pass OTM trades that would have been dropped by
        the old 1.5× penalisation when premium falls between 1.0–1.5× floor.
  AC-2  Callers that explicitly pass deep_otm_multiplier=1.5 must retain the
        old behaviour — the parameter is honoured, not ignored.
  AC-3  At-the-money (ATM) / ITM trades are unaffected: their score path
        never enters the deep-OTM branch regardless of the multiplier.
  E-1   Default multiplier regression: ingest_tick() with a deep-OTM trade
        whose premium is between 1.0×–1.5× the DTE tier floor must NOT be
        dropped under the new default (1.0).
  E-2   Explicit-1.5 regression: same trade under deep_otm_multiplier=1.5
        must be dropped (the old behaviour is preserved for explicit callers).
  E-3   ATM neutrality: an ATM trade is unaffected by either multiplier.

Test IDs: E-01 … E-10

Panel deliberation findings (ING-005 pre-merge, 2026-05-03):
  SA-Q1 / SA-Q2: multiplier applies only inside _classify_otm(); changing the
    default from 1.5 → 1.0 widens the acceptance window without changing any
    other gate (DTE floors, premium floors, min_trades).
  PBE-Q1: deep-OTM boundary for default = floor × 1.0.  Any premium ≥ floor
    passes; old boundary was floor × 1.5.
  PBE-Q2: explicit 1.5 callers (if any) retain old behaviour via the kwarg.
  QA-Q1: existing test_classify_otm assertions remain valid — they do not
    hard-code a 1.5 default, they pass the multiplier explicitly.
  QA-Q2: OI pipeline and composite-signal engine are not affected (they do
    not call _classify_otm() directly).
  QA-Q3: no cross-test pollution — each test builds a fresh accumulator.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    ticker="SPYTICKER",
    contract_type="CALL",
    strike=600.0,
    expiry="2026-06-20",
    premium=60_000.0,
    dte=5,
    underlying_price=500.0,   # strike 600 > price 500 → deep OTM for a call
    trade_type="TRADE",
    order_side="UNKNOWN",
):
    """
    Build a dict-form event for accumulator tests.

    Default geometry: call with strike=600 vs underlying=500 (20 % OTM).
    That is deep-OTM territory for any reasonable OTM classifier threshold.
    Change `underlying_price` / `strike` to push in/out of OTM region.
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
        "order_side":       order_side,
        "timestamp":        datetime.now(timezone.utc),
    }


def _fresh_acc(deep_otm_multiplier=None, tier=2, **kwargs):
    """
    Return a fresh RepetitionAccumulator.

    - min_trades=1 removes Gate-1 (count) as a confounding variable.
    - tier=2 (T2 default via set_tier_map) so _DEFAULT_DTE_PREMIUM_TIERS
      T2 floor for DTE≤7 = $25,000 is the relevant boundary.
    - deep_otm_multiplier forwarded only when explicitly provided so we can
      test the true default path (no kwarg at all) in E-1.
    """
    init_kwargs = dict(
        window_minutes=30,
        min_trades=1,
        min_premium=10_000,
        dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
        **kwargs,
    )
    if deep_otm_multiplier is not None:
        init_kwargs["deep_otm_multiplier"] = deep_otm_multiplier

    acc = RepetitionAccumulator(**init_kwargs)
    acc.set_tier_map({"SPYTICKER": tier})
    return acc


# ── E-01  Default multiplier: borderline deep-OTM trade passes ───────────────

def test_E01_default_multiplier_borderline_deep_otm_passes():
    """
    E-1 — core regression for ING-005.

    Setup:
      - Fresh accumulator, default deep_otm_multiplier (1.0).
      - SPYTICKER assigned T2 via set_tier_map → T2 floor for DTE≤7 = $25,000.
      - DTE=5, premium=$28,000 (deep OTM: strike=600, underlying=500).

    Old behaviour (multiplier=1.5): effective OTM floor = $25,000 × 1.5 = $37,500.
      $28,000 < $37,500 → trade was dropped.

    New behaviour (multiplier=1.0): effective OTM floor = $25,000 × 1.0 = $25,000.
      $28,000 ≥ $25,000 → trade must pass (not None).

    Failure here means the default was NOT changed from 1.5 to 1.0.
    """
    acc = _fresh_acc()   # no explicit multiplier — tests the new default
    ev  = _make_event(premium=28_000.0, dte=5)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is not None, (
        f"ING-005: premium=$28k, T2 floor=$25k, default deep_otm_multiplier must be "
        f"1.0 so $28k >= $25k passes. Got None. The default is still 1.5 or the gate "
        f"is using the old effective floor of $37,500."
    )
    assert result.total_premium == pytest.approx(28_000.0), (
        f"Episode total_premium must be $28,000. Got {result.total_premium}"
    )


# ── E-02  Explicit multiplier=1.5 retains old drop behaviour ─────────────────

def test_E02_explicit_1_5_multiplier_drops_borderline_deep_otm():
    """
    E-2 — backwards-compat proof for ING-005.

    Same scenario as E-01 but deep_otm_multiplier=1.5 passed explicitly.
    Effective OTM floor = $25,000 × 1.5 = $37,500.
    $28,000 < $37,500 → must be dropped (None).

    This confirms the parameter is actively honoured: a caller that relied
    on the old default can restore 1.5× behaviour via an explicit kwarg.
    If this test fails, the multiplier is ignored entirely.
    """
    acc = _fresh_acc(deep_otm_multiplier=1.5)
    ev  = _make_event(premium=28_000.0, dte=5)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is None, (
        f"ING-005: premium=$28k, T2 floor=$25k, explicit deep_otm_multiplier=1.5 → "
        f"effective floor=$37,500. $28k < $37,500 must be dropped. "
        f"Got {result!r}. Multiplier kwarg is not being honoured."
    )


# ── E-03  ATM trade unaffected by either multiplier ───────────────────────────

def test_E03_atm_trade_unaffected_by_multiplier():
    """
    E-3 — neutrality proof for ING-005.

    An at-the-money call (strike == underlying_price) is not deep OTM.
    Its score path must not enter the deep-OTM penalty branch.
    Both multiplier=1.0 (default) and multiplier=1.5 must yield a passing
    result for a qualifying ATM premium.

    Setup:
      - strike=500, underlying_price=500 (ATM call).
      - DTE=5, premium=$60,000 (well above T2 floor of $25,000).
    """
    for multiplier in (1.0, 1.5):
        acc = _fresh_acc(deep_otm_multiplier=multiplier)
        ev  = _make_event(
            premium=60_000.0,
            dte=5,
            strike=500.0,
            underlying_price=500.0,   # ATM
        )
        result = asyncio.run(acc.ingest_tick(ev))
        assert result is not None, (
            f"ATM trade with premium=$60k, multiplier={multiplier}: "
            f"must not be affected by deep-OTM branch. Got None."
        )
        assert result.total_premium == pytest.approx(60_000.0)


# ── E-04  Default multiplier boundary: at-floor deep-OTM passes ──────────────

def test_E04_default_at_floor_deep_otm_passes():
    """
    Boundary precision for E-01: premium exactly at T2 floor ($25,000).

    With multiplier=1.0, effective OTM floor = $25,000 × 1.0 = $25,000.
    premium=$25,000 == floor → must pass (not dropped).
    """
    acc = _fresh_acc()
    ev  = _make_event(premium=25_000.0, dte=5)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is not None, (
        f"premium=$25k == T2 floor, multiplier=1.0: at-floor must pass. Got None."
    )


# ── E-05  Default multiplier: sub-floor deep-OTM still dropped ───────────────

def test_E05_default_sub_floor_deep_otm_dropped():
    """
    ING-005 only raises the acceptance window — it does not remove the DTE
    tier gate.  A premium below the floor ($24,999 < $25,000) must still be
    dropped regardless of the multiplier being 1.0.

    This guards against an over-broad fix that accidentally disables the gate.
    """
    acc = _fresh_acc()
    ev  = _make_event(premium=24_999.0, dte=5)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is None, (
        f"premium=$24,999 < T2 floor $25,000, multiplier=1.0: must still be dropped. "
        f"Got {result!r}. ING-005 over-broadly disabled the DTE tier gate."
    )


# ── E-06  Explicit multiplier=1.0 is identical to default ────────────────────

def test_E06_explicit_1_0_same_as_default():
    """
    Confirm deep_otm_multiplier=1.0 (explicit) produces identical behaviour
    to the new default.  Borderline $28k trade must pass in both cases.
    """
    acc_default  = _fresh_acc()
    acc_explicit = _fresh_acc(deep_otm_multiplier=1.0)

    ev = _make_event(premium=28_000.0, dte=5)

    r_default  = asyncio.run(acc_default.ingest_tick(ev))
    r_explicit = asyncio.run(acc_explicit.ingest_tick(_make_event(premium=28_000.0, dte=5)))

    both_pass = (r_default is not None) and (r_explicit is not None)
    assert both_pass, (
        f"explicit 1.0 and default must behave identically. "
        f"default→{r_default!r}, explicit→{r_explicit!r}"
    )


# ── E-07  T1 unknown ticker: default multiplier does not widen T1 gate ────────

def test_E07_unknown_ticker_t1_gate_not_widened():
    """
    ING-005 targets the deep_otm_multiplier only.  The T1 default for unknown
    tickers (DTE≤7 floor = $50,000) must be unaffected.

    premium=$45,000, DTE=5, unknown ticker (T1) must still be dropped.
    This was dropped before ING-005 and must remain dropped after.
    """
    # Use default accumulator, no set_tier_map → unknown ticker → T1
    acc = RepetitionAccumulator(
        window_minutes=30,
        min_trades=1,
        min_premium=10_000,
        dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
    )
    ev = _make_event(ticker="UNKNOWNTICKER", premium=45_000.0, dte=5)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is None, (
        f"T1 unknown ticker, DTE=5, premium=$45k < T1 floor $50k: "
        f"must still be dropped after ING-005. Got {result!r}. "
        f"ING-005 incorrectly widened the T1 gate."
    )


# ── E-08  T1 unknown ticker still passes above floor ────────────────────────

def test_E08_unknown_ticker_t1_passes_above_floor():
    """
    Companion to E-07: unknown ticker with premium=$60k, DTE=5 must pass
    (T1 floor for DTE≤7 = $50,000; $60k >= $50k).
    """
    acc = RepetitionAccumulator(
        window_minutes=30,
        min_trades=1,
        min_premium=10_000,
        dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
    )
    ev = _make_event(ticker="UNKNOWNTICKER", premium=60_000.0, dte=5)
    result = asyncio.run(acc.ingest_tick(ev))
    assert result is not None, (
        f"T1 unknown ticker, DTE=5, premium=$60k >= T1 floor $50k: must pass. "
        f"Got None."
    )


# ── E-09  Multiplier does not affect long-DTE bucket ────────────────────────

def test_E09_long_dte_bucket_unaffected_by_multiplier():
    """
    For DTE > 45 the accumulator uses a different tier-floor row.
    Confirm that changing deep_otm_multiplier from 1.0 → 1.5 does not
    inadvertently break the long-DTE bucket for an above-floor premium.

    premium=$100,000, DTE=60 (long-dated), SPYTICKER=T2.
    Both multipliers must pass this trade.
    """
    for multiplier in (1.0, 1.5):
        acc = _fresh_acc(deep_otm_multiplier=multiplier)
        ev  = _make_event(premium=100_000.0, dte=60)
        result = asyncio.run(acc.ingest_tick(ev))
        assert result is not None, (
            f"Long-DTE bucket, multiplier={multiplier}, premium=$100k: "
            f"must pass. Got None."
        )


# ── E-10  No cross-test pollution (accumulator state isolation) ───────────────

def test_E10_accumulator_state_isolation():
    """
    Each test in this file builds a fresh accumulator.  This explicit isolation
    test confirms that a passing result from a previous ingest does not
    contaminate a freshly constructed accumulator.

    Run two fresh accumulators sequentially with the same event and assert
    both produce identical results.
    """
    ev = _make_event(premium=28_000.0, dte=5)

    acc_a = _fresh_acc()
    acc_b = _fresh_acc()

    result_a = asyncio.run(acc_a.ingest_tick(ev))
    result_b = asyncio.run(acc_b.ingest_tick(_make_event(premium=28_000.0, dte=5)))

    assert (result_a is None) == (result_b is None), (
        f"State isolation failure: acc_a→{result_a!r}, acc_b→{result_b!r}. "
        f"Results must be identical for fresh accumulators with the same input."
    )
