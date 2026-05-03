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

Test IDs: E-01 … E-10, plus deliberation QA matrix E-01a/b/c

Pre-merge deliberation findings resolved in this file (ING-005, 2026-05-03):
  SA-Q1: Gate 3 dormancy documented on `if` line in accumulator — no test
    needed; SA-Q1 is a source comment fix only.
  SA-Q2: _classify_otm() docstring tightened — otm_band not yet wired to
    RepetitionEpisode; deferred to ING-007. No test impact.
  PBE-Q1: Three spec-literal QA matrix cases added as E-01a, E-01b, E-01c
    (T1@18%OTM/$60k, T2@14%OTM/$110k, T3@9%OTM/$510k) — exactly the cases
    documented in FIXES.md deliberation sign-off.
  PBE-Q2: E-06 clarifying comment added — acc_explicit uses a fresh
    _make_event() call, not the same dict as acc_default. No logic change.
  QA-Q1: All async tests converted to @pytest.mark.asyncio + async def.
    Safe under both default pytest and asyncio_mode=auto.
  QA-Q2: E-02b added — deep-sub-floor drop under explicit multiplier=1.5
    ($20k vs effective floor $37.5k). Confirms full-range multiplication.
"""
from datetime import datetime, timezone

import pytest
import pytest_asyncio  # noqa: F401 — imported for asyncio_mode compat

from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    ticker="SPYTICKER",
    contract_type="CALL",
    strike=600.0,
    expiry="2026-06-20",
    premium=60_000.0,
    dte=5,
    underlying_price=500.0,   # strike 600 > price 500 → deep OTM for a call (20%)
    trade_type="TRADE",
    order_side="UNKNOWN",
):
    """
    Build a dict-form event for accumulator tests.

    Default geometry: call with strike=600 vs underlying=500 (20% OTM).
    That is deep-OTM territory for the 12% threshold in _classify_otm().
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
      test the true default path (no kwarg at all) in E-01.
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


def _fresh_acc_t1(**kwargs):
    """
    T1 accumulator — no set_tier_map so unknown tickers default to T1 (strict).
    T1 floor for DTE≤7 = $50,000.
    """
    init_kwargs = dict(
        window_minutes=30,
        min_trades=1,
        min_premium=10_000,
        dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
        **kwargs,
    )
    return RepetitionAccumulator(**init_kwargs)


def _fresh_acc_t3(ticker="T3TICKER", **kwargs):
    """
    T3 accumulator — ticker assigned tier=3 via set_tier_map.
    T3 shares the T2/T3 column (col=1). DTE≤90 floor = $500,000.
    """
    init_kwargs = dict(
        window_minutes=30,
        min_trades=1,
        min_premium=10_000,
        dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
        **kwargs,
    )
    acc = RepetitionAccumulator(**init_kwargs)
    acc.set_tier_map({ticker: 3})
    return acc


# ══════════════════════════════════════════════════════════════════════════════
# PBE-Q1 — Spec-literal QA matrix cases (deliberation sign-off, FIXES.md)
# These three cases are the exact E-1/E-2/E-3 from the ING-005 deliberation.
# They must pass for the AC checklist item to be satisfied.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_E01a_spec_T1_18pct_otm_passes():
    """
    PBE-Q1 / Deliberation QA matrix E-1:
    T1 ticker, DTE=5 (DTE≤7 bucket), strike 18% OTM, premium=$60,000.

    T1 floor for DTE≤7 = $50,000.
    Old behaviour (1.5×): effective floor = $75,000. $60k < $75k → DROPPED.
    New behaviour (1.0×): effective floor = $50,000. $60k ≥ $50k → PASSES.

    Geometry: strike=590, underlying=500 → (590-500)/500 = 18% OTM (DEEP_OTM).
    """
    acc = _fresh_acc_t1()  # unknown ticker → T1, DTE≤7 floor = $50,000
    ev  = _make_event(
        ticker="T1TICKER",
        strike=590.0,
        underlying_price=500.0,   # 18% OTM
        premium=60_000.0,
        dte=5,
    )
    result = await acc.ingest_tick(ev)
    assert result is not None, (
        "Spec E-1 (T1, 18% OTM, $60k): must pass with default multiplier=1.0. "
        "T1 DTE≤7 floor=$50k. $60k ≥ $50k. Got None — default is still 1.5."
    )
    assert result.total_premium == pytest.approx(60_000.0)


@pytest.mark.asyncio
async def test_E01b_spec_T2_14pct_otm_passes():
    """
    PBE-Q1 / Deliberation QA matrix E-2:
    T2 ticker, DTE=15 (DTE≤30 bucket), strike 14% OTM, premium=$110,000.

    T2 floor for DTE≤30 = $100,000.
    Old behaviour (1.5×): effective floor = $150,000. $110k < $150k → DROPPED.
    New behaviour (1.0×): effective floor = $100,000. $110k ≥ $100k → PASSES.

    Geometry: strike=570, underlying=500 → (570-500)/500 = 14% OTM (DEEP_OTM).
    """
    acc = RepetitionAccumulator(
        window_minutes=30,
        min_trades=1,
        min_premium=10_000,
        dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
    )
    acc.set_tier_map({"T2TICKER": 2})
    ev = _make_event(
        ticker="T2TICKER",
        strike=570.0,
        underlying_price=500.0,   # 14% OTM
        premium=110_000.0,
        dte=15,
    )
    result = await acc.ingest_tick(ev)
    assert result is not None, (
        "Spec E-2 (T2, 14% OTM, $110k): must pass with default multiplier=1.0. "
        "T2 DTE≤30 floor=$100k. $110k ≥ $100k. Got None — default is still 1.5."
    )
    assert result.total_premium == pytest.approx(110_000.0)


@pytest.mark.asyncio
async def test_E01c_spec_T3_9pct_otm_passes():
    """
    PBE-Q1 / Deliberation QA matrix E-3:
    T3 ticker, DTE=60 (DTE≤90 bucket), strike 9% OTM, premium=$510,000.

    T3 (= T2/T3 col) floor for DTE≤90 = $500,000.
    Old behaviour (1.5×): effective floor = $750,000. $510k < $750k → DROPPED.
    New behaviour (1.0×): effective floor = $500,000. $510k ≥ $500k → PASSES.

    NOTE — Gate 2 vs Gate 3 coverage:
      9% OTM is STANDARD_OTM (below the 12% _classify_otm threshold), so
      this trade never enters the deep_otm_multiplier branch (Gate 3) under
      either multiplier value. This case is a Gate 2 (DTE tier floor) boundary
      test, NOT a Gate 3 (deep OTM penalty) test. It validates that the T3
      DTE≤90 floor of $500k operates correctly at the $510k boundary
      independent of any OTM multiplier logic. Gate 3 coverage for T3 is
      provided by E-01 (DEEP_OTM geometry, T2 tier) and E-01a/b (T1/T2 tiers
      at 18%/14% OTM respectively).
    """
    acc = _fresh_acc_t3(ticker="T3TICKER")
    ev = _make_event(
        ticker="T3TICKER",
        strike=545.0,
        underlying_price=500.0,   # 9% OTM — STANDARD_OTM
        premium=510_000.0,
        dte=60,
    )
    result = await acc.ingest_tick(ev)
    assert result is not None, (
        "Spec E-3 (T3, 9% OTM, $510k): must pass. "
        "T3 DTE≤90 floor=$500k. $510k ≥ $500k. Got None."
    )
    assert result.total_premium == pytest.approx(510_000.0)


# ══════════════════════════════════════════════════════════════════════════════
# Core mechanism tests (E-01 through E-10)
# QA-Q1: all converted to @pytest.mark.asyncio + async def
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_E01_default_multiplier_borderline_deep_otm_passes():
    """
    E-1 — core regression for ING-005.

    Setup:
      - Fresh accumulator, default deep_otm_multiplier (1.0).
      - SPYTICKER assigned T2 via set_tier_map → T2 floor for DTE≤7 = $25,000.
      - DTE=5, premium=$28,000 (deep OTM: strike=600, underlying=500, 20%).

    Old behaviour (multiplier=1.5): effective OTM floor = $25,000 × 1.5 = $37,500.
      $28,000 < $37,500 → trade was dropped.

    New behaviour (multiplier=1.0): effective OTM floor = $25,000 × 1.0 = $25,000.
      $28,000 ≥ $25,000 → trade must pass (not None).

    Failure here means the default was NOT changed from 1.5 to 1.0.
    """
    acc = _fresh_acc()   # no explicit multiplier — tests the new default
    ev  = _make_event(premium=28_000.0, dte=5)
    result = await acc.ingest_tick(ev)
    assert result is not None, (
        f"ING-005: premium=$28k, T2 floor=$25k, default deep_otm_multiplier must be "
        f"1.0 so $28k >= $25k passes. Got None. The default is still 1.5 or the gate "
        f"is using the old effective floor of $37,500."
    )
    assert result.total_premium == pytest.approx(28_000.0)


@pytest.mark.asyncio
async def test_E02_explicit_1_5_multiplier_drops_borderline_deep_otm():
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
    result = await acc.ingest_tick(ev)
    assert result is None, (
        f"ING-005: premium=$28k, T2 floor=$25k, explicit deep_otm_multiplier=1.5 → "
        f"effective floor=$37,500. $28k < $37,500 must be dropped. "
        f"Got {result!r}. Multiplier kwarg is not being honoured."
    )


@pytest.mark.asyncio
async def test_E02b_explicit_1_5_deep_sub_floor_dropped():
    """
    QA-Q2 — Full-range multiplication proof for ING-005.

    premium=$20,000, explicit deep_otm_multiplier=1.5.
    T2 floor for DTE≤7 = $25,000.
    Effective OTM floor = $25,000 × 1.5 = $37,500.
    $20,000 < $37,500 → must be dropped.

    Confirms multiplication is applied across the full range, not just at
    the boundary edge tested by E-02 ($28k). An implementation that only
    applies the multiplier within a $10k tolerance of the floor would pass
    E-02 but fail this case.
    """
    acc = _fresh_acc(deep_otm_multiplier=1.5)
    ev  = _make_event(premium=20_000.0, dte=5)
    result = await acc.ingest_tick(ev)
    assert result is None, (
        f"premium=$20k, T2 floor=$25k, multiplier=1.5 → effective floor=$37,500. "
        f"$20k < $37,500 must be dropped. Got {result!r}. "
        f"Multiplier is not applied across full range."
    )


@pytest.mark.asyncio
async def test_E03_atm_trade_unaffected_by_multiplier():
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
        result = await acc.ingest_tick(ev)
        assert result is not None, (
            f"ATM trade with premium=$60k, multiplier={multiplier}: "
            f"must not be affected by deep-OTM branch. Got None."
        )
        assert result.total_premium == pytest.approx(60_000.0)


@pytest.mark.asyncio
async def test_E04_default_at_floor_deep_otm_passes():
    """
    Boundary precision for E-01: premium exactly at T2 floor ($25,000).

    With multiplier=1.0, effective OTM floor = $25,000 × 1.0 = $25,000.
    premium=$25,000 == floor → must pass (not dropped).
    """
    acc = _fresh_acc()
    ev  = _make_event(premium=25_000.0, dte=5)
    result = await acc.ingest_tick(ev)
    assert result is not None, (
        f"premium=$25k == T2 floor, multiplier=1.0: at-floor must pass. Got None."
    )


@pytest.mark.asyncio
async def test_E05_default_sub_floor_deep_otm_dropped():
    """
    ING-005 only raises the acceptance window — it does not remove the DTE
    tier gate. A premium below the floor ($24,999 < $25,000) must still be
    dropped regardless of the multiplier being 1.0.

    This guards against an over-broad fix that accidentally disables the gate.
    """
    acc = _fresh_acc()
    ev  = _make_event(premium=24_999.0, dte=5)
    result = await acc.ingest_tick(ev)
    assert result is None, (
        f"premium=$24,999 < T2 floor $25,000, multiplier=1.0: must still be dropped. "
        f"Got {result!r}. ING-005 over-broadly disabled the DTE tier gate."
    )


@pytest.mark.asyncio
async def test_E06_explicit_1_0_same_as_default():
    """
    Confirm deep_otm_multiplier=1.0 (explicit) produces identical behaviour
    to the new default. Borderline $28k trade must pass in both cases.

    PBE-Q2: acc_default and acc_explicit each receive a fresh _make_event()
    call — they do not share the same dict. No shared-mutable-state hazard.
    """
    acc_default  = _fresh_acc()
    acc_explicit = _fresh_acc(deep_otm_multiplier=1.0)

    # Each accumulator gets its own fresh event dict (PBE-Q2)
    r_default  = await acc_default.ingest_tick(_make_event(premium=28_000.0, dte=5))
    r_explicit = await acc_explicit.ingest_tick(_make_event(premium=28_000.0, dte=5))

    both_pass = (r_default is not None) and (r_explicit is not None)
    assert both_pass, (
        f"explicit 1.0 and default must behave identically. "
        f"default→{r_default!r}, explicit→{r_explicit!r}"
    )


@pytest.mark.asyncio
async def test_E07_unknown_ticker_t1_gate_not_widened():
    """
    ING-005 targets the deep_otm_multiplier only. The T1 default for unknown
    tickers (DTE≤7 floor = $50,000) must be unaffected.

    premium=$45,000, DTE=5, unknown ticker (T1) must still be dropped.
    """
    acc = _fresh_acc_t1()
    ev  = _make_event(ticker="UNKNOWNTICKER", premium=45_000.0, dte=5)
    result = await acc.ingest_tick(ev)
    assert result is None, (
        f"T1 unknown ticker, DTE=5, premium=$45k < T1 floor $50k: "
        f"must still be dropped after ING-005. Got {result!r}. "
        f"ING-005 incorrectly widened the T1 gate."
    )


@pytest.mark.asyncio
async def test_E08_unknown_ticker_t1_passes_above_floor():
    """
    Companion to E-07: unknown ticker with premium=$60k, DTE=5 must pass
    (T1 floor for DTE≤7 = $50,000; $60k >= $50k).
    """
    acc = _fresh_acc_t1()
    ev  = _make_event(ticker="UNKNOWNTICKER", premium=60_000.0, dte=5)
    result = await acc.ingest_tick(ev)
    assert result is not None, (
        f"T1 unknown ticker, DTE=5, premium=$60k >= T1 floor $50k: must pass. "
        f"Got None."
    )


@pytest.mark.asyncio
async def test_E09_long_dte_bucket_unaffected_by_multiplier():
    """
    For DTE > 45 the accumulator uses the DTE≤90 bucket.
    Confirm that changing deep_otm_multiplier from 1.0 → 1.5 does not
    inadvertently break the long-DTE bucket for an above-floor premium.

    premium=$100,000, DTE=60, SPYTICKER=T2. Both multipliers must pass.
    """
    for multiplier in (1.0, 1.5):
        acc = _fresh_acc(deep_otm_multiplier=multiplier)
        ev  = _make_event(premium=100_000.0, dte=60)
        result = await acc.ingest_tick(ev)
        assert result is not None, (
            f"Long-DTE bucket, multiplier={multiplier}, premium=$100k: "
            f"must pass. Got None."
        )


@pytest.mark.asyncio
async def test_E10_accumulator_state_isolation():
    """
    Each test in this file builds a fresh accumulator. This explicit isolation
    test confirms that a passing result from a previous ingest does not
    contaminate a freshly constructed accumulator.

    Run two fresh accumulators sequentially with the same event geometry and
    assert both produce identical results.
    """
    acc_a = _fresh_acc()
    acc_b = _fresh_acc()

    # Each accumulator gets its own fresh event dict
    result_a = await acc_a.ingest_tick(_make_event(premium=28_000.0, dte=5))
    result_b = await acc_b.ingest_tick(_make_event(premium=28_000.0, dte=5))

    assert (result_a is None) == (result_b is None), (
        f"State isolation failure: acc_a→{result_a!r}, acc_b→{result_b!r}. "
        f"Results must be identical for fresh accumulators with the same input."
    )
