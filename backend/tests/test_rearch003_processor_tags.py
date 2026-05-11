"""
test_rearch003_processor_tags.py

REARCH-003: 100% coverage for the new processor.py enrichment layer.

Functions under test:
  _compute_dte_bucket(dte)              -> str
  _compute_notional_tier(premium)       -> str
  _compute_cipher_score(...)            -> int
  IngestionProcessor.enrich_tags(ev_dict) -> dict

Design principles:
  - Pure-function tests: no I/O, no mocks needed.
  - Every branch, boundary value, and None-safety guard exercised.
  - enrich_tags() tests verify both mutation-in-place and return value,
    correct score wiring, and that missing keys default to zero-score.
  - DTE bucket names match processor.py constants exactly
    ('0DTE', '1-4', '5-60', '61-90', '90+').
  - Notional tiers match processor.py constants
    ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN').
  - Score range is 0-4 (Dim 5 is REARCH-004 scope).

Bootstrap strategy:
  processor.py has module-level imports of supabase_client, chain_store, etc.
  Those are only used inside async functions that are never called here.
  We stub them via sys.modules ONLY when they are not already importable, so
  the real ingestion package (present in CI's sys.path as backend/) is never
  shadowed.  The pure functions under test never reach those stubs.

All tests run offline -- no Supabase or Tradier connectivity required.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: stub heavy external deps only when not already importable.
# Running under pytest from backend/ means `ingestion` is the real package —
# never replace it.  Only absent modules need stubs.
# ---------------------------------------------------------------------------

def _ensure_stub(name: str) -> None:
    """Register a no-op stub in sys.modules only if the module isn't present."""
    if name not in sys.modules:
        mod = types.ModuleType(name)
        sys.modules[name] = mod


for _absent in [
    "supabase",
    "postgrest",
    "services.supabase_client",
    "services.chain_store",
    "services.gate_config_store",
    "core.event_bus",
]:
    _ensure_stub(_absent)

# Ensure ingestion.symbol_registry exists as a stub only if the real one is absent.
# Under normal CI (pytest from backend/) the real module is importable; this is a
# safety net for environments where it is not yet on sys.path.
_ensure_stub("ingestion.symbol_registry")

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")

from ingestion.processor import (          # noqa: E402  (import after sys.modules patch)
    _compute_dte_bucket,
    _compute_notional_tier,
    _compute_cipher_score,
    IngestionProcessor,
)


# ===========================================================================
# CHUNK 1 — _compute_dte_bucket
# ===========================================================================

class TestComputeDteBucket:
    """
    Boundary table (from processor.py constants):
      dte=None / dte<0  -> '90+'   (conservative: no near-term signal)
      dte=0             -> '0DTE'
      dte=1..4          -> '1-4'   (_DTE_NEAR_MAX = 4)
      dte=5..60         -> '5-60'  (_DTE_MID_MAX  = 60)
      dte=61..90        -> '61-90' (_DTE_FAR_MAX  = 90)
      dte>=91           -> '90+'
    """

    def test_none_returns_90plus(self):
        assert _compute_dte_bucket(None) == "90+"

    def test_negative_returns_90plus(self):
        assert _compute_dte_bucket(-1) == "90+"

    def test_large_negative_returns_90plus(self):
        assert _compute_dte_bucket(-999) == "90+"

    def test_zero_returns_0dte(self):
        assert _compute_dte_bucket(0) == "0DTE"

    def test_1_returns_1_4(self):
        assert _compute_dte_bucket(1) == "1-4"

    def test_4_returns_1_4(self):
        """Upper boundary of 1-4 bucket (inclusive)."""
        assert _compute_dte_bucket(4) == "1-4"

    def test_5_returns_5_60(self):
        """Lower boundary of 5-60 bucket."""
        assert _compute_dte_bucket(5) == "5-60"

    def test_30_returns_5_60(self):
        assert _compute_dte_bucket(30) == "5-60"

    def test_60_returns_5_60(self):
        """Upper boundary of 5-60 bucket (inclusive)."""
        assert _compute_dte_bucket(60) == "5-60"

    def test_61_returns_61_90(self):
        """Lower boundary of 61-90 bucket."""
        assert _compute_dte_bucket(61) == "61-90"

    def test_90_returns_61_90(self):
        """Upper boundary of 61-90 bucket (inclusive)."""
        assert _compute_dte_bucket(90) == "61-90"

    def test_91_returns_90plus(self):
        """First value above 61-90 bucket falls to 90+."""
        assert _compute_dte_bucket(91) == "90+"

    def test_365_returns_90plus(self):
        assert _compute_dte_bucket(365) == "90+"

    def test_return_type_is_str(self):
        for dte in [None, -1, 0, 1, 4, 5, 60, 61, 90, 91, 200]:
            assert isinstance(_compute_dte_bucket(dte), str), f"Expected str for dte={dte}"


# ===========================================================================
# CHUNK 2 — _compute_notional_tier
# ===========================================================================

class TestComputeNotionalTier:
    """
    Threshold table (inclusive lower bounds, from processor.py constants):
      >= 500_000  -> 'GOLDEN'      (_NOTIONAL_GOLDEN)
      >= 100_000  -> 'BLOCK'       (_NOTIONAL_BLOCK)
      >= 50_000   -> 'NOTEWORTHY'  (_NOTIONAL_NOTEWORTHY)
      < 50_000    -> 'WATCH'
      None / 0.0  -> 'WATCH'       (p = premium or 0.0)
    """

    def test_none_returns_watch(self):
        assert _compute_notional_tier(None) == "WATCH"

    def test_zero_returns_watch(self):
        assert _compute_notional_tier(0.0) == "WATCH"

    def test_below_noteworthy_returns_watch(self):
        assert _compute_notional_tier(49_999.99) == "WATCH"

    def test_exactly_noteworthy_floor_returns_noteworthy(self):
        """$50k exactly is the NOTEWORTHY lower boundary."""
        assert _compute_notional_tier(50_000.0) == "NOTEWORTHY"

    def test_mid_noteworthy_returns_noteworthy(self):
        assert _compute_notional_tier(75_000.0) == "NOTEWORTHY"

    def test_just_below_block_returns_noteworthy(self):
        assert _compute_notional_tier(99_999.99) == "NOTEWORTHY"

    def test_exactly_block_floor_returns_block(self):
        """$100k exactly is the BLOCK lower boundary."""
        assert _compute_notional_tier(100_000.0) == "BLOCK"

    def test_mid_block_returns_block(self):
        assert _compute_notional_tier(250_000.0) == "BLOCK"

    def test_just_below_golden_returns_block(self):
        assert _compute_notional_tier(499_999.99) == "BLOCK"

    def test_exactly_golden_floor_returns_golden(self):
        """$500k exactly is the GOLDEN lower boundary."""
        assert _compute_notional_tier(500_000.0) == "GOLDEN"

    def test_above_golden_returns_golden(self):
        assert _compute_notional_tier(1_000_000.0) == "GOLDEN"

    def test_return_type_is_str(self):
        for p in [None, 0, 10_000, 50_000, 100_000, 500_000, 999_999]:
            assert isinstance(_compute_notional_tier(p), str), f"Expected str for premium={p}"


# ===========================================================================
# CHUNK 3 — _compute_cipher_score
# ===========================================================================

class TestComputeCipherScore:
    """
    Four binary dimensions, 1 point each. Range: 0-4.

    Dim 1 — is_ask_side == True
    Dim 2 — vol_oi_signal == 'HIGH'
    Dim 3 — notional_tier in ('BLOCK', 'GOLDEN')
    Dim 4 — dte_bucket in ('1-4', '5-60')

    None-safety: all inputs coerced to their zero-score defaults if None.
    """

    # ---- zero-score cases ------------------------------------------------

    def test_all_zero_score(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="WATCH", dte_bucket="0DTE",
        ) == 0

    def test_zero_score_leaps(self):
        """90+ bucket and NOTEWORTHY tier both fail to score."""
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="UNKNOWN",
            notional_tier="NOTEWORTHY", dte_bucket="90+",
        ) == 0

    # ---- each dimension individually ------------------------------------

    def test_dim1_ask_side_adds_1(self):
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal="NORMAL",
            notional_tier="WATCH", dte_bucket="90+",
        ) == 1

    def test_dim2_vol_oi_high_adds_1(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="HIGH",
            notional_tier="WATCH", dte_bucket="90+",
        ) == 1

    def test_dim3_block_adds_1(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="BLOCK", dte_bucket="90+",
        ) == 1

    def test_dim3_golden_adds_1(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="GOLDEN", dte_bucket="90+",
        ) == 1

    def test_dim4_1_4_adds_1(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="WATCH", dte_bucket="1-4",
        ) == 1

    def test_dim4_5_60_adds_1(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="WATCH", dte_bucket="5-60",
        ) == 1

    # ---- non-scoring buckets / tiers ------------------------------------

    def test_dim4_0dte_does_not_score(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="WATCH", dte_bucket="0DTE",
        ) == 0

    def test_dim4_61_90_does_not_score(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="WATCH", dte_bucket="61-90",
        ) == 0

    def test_dim3_noteworthy_does_not_score(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="NOTEWORTHY", dte_bucket="90+",
        ) == 0

    def test_dim3_watch_does_not_score(self):
        assert _compute_cipher_score(
            is_ask_side=False, vol_oi_signal="NORMAL",
            notional_tier="WATCH", dte_bucket="90+",
        ) == 0

    # ---- max score -------------------------------------------------------

    def test_max_score_4(self):
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal="HIGH",
            notional_tier="GOLDEN", dte_bucket="1-4",
        ) == 4

    def test_max_score_4_with_block_and_5_60(self):
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal="HIGH",
            notional_tier="BLOCK", dte_bucket="5-60",
        ) == 4

    # ---- partial scores --------------------------------------------------

    def test_score_2_ask_side_plus_vol_oi(self):
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal="HIGH",
            notional_tier="WATCH", dte_bucket="90+",
        ) == 2

    def test_score_3_missing_only_dte(self):
        """61-90 bucket doesn't score Dim 4 -> only Dims 1/2/3 fire."""
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal="HIGH",
            notional_tier="GOLDEN", dte_bucket="61-90",
        ) == 3

    # ---- None-safety (must never raise) ---------------------------------

    def test_none_is_ask_side_treated_as_false(self):
        """None is falsy -> Dim 1 scores 0; Dims 2/3/4 still score."""
        assert _compute_cipher_score(
            is_ask_side=None, vol_oi_signal="HIGH",
            notional_tier="GOLDEN", dte_bucket="1-4",
        ) == 3

    def test_none_vol_oi_signal_treated_as_unknown(self):
        """None coerced to 'UNKNOWN' -> Dim 2 scores 0; Dims 1/3/4 score."""
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal=None,
            notional_tier="GOLDEN", dte_bucket="1-4",
        ) == 3

    def test_none_notional_tier_treated_as_watch(self):
        """None coerced to 'WATCH' -> Dim 3 scores 0; Dims 1/2/4 score."""
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal="HIGH",
            notional_tier=None, dte_bucket="1-4",
        ) == 3

    def test_none_dte_bucket_treated_as_90plus(self):
        """None coerced to '90+' -> Dim 4 scores 0; Dims 1/2/3 score."""
        assert _compute_cipher_score(
            is_ask_side=True, vol_oi_signal="HIGH",
            notional_tier="GOLDEN", dte_bucket=None,
        ) == 3

    def test_all_none_inputs_returns_zero(self):
        assert _compute_cipher_score(
            is_ask_side=None, vol_oi_signal=None,
            notional_tier=None, dte_bucket=None,
        ) == 0

    def test_return_type_is_int(self):
        for ask, vol, tier, dte in [
            (True,  "HIGH",   "GOLDEN", "1-4"),
            (False, "NORMAL", "WATCH",  "90+"),
            (None,  None,     None,     None),
        ]:
            result = _compute_cipher_score(ask, vol, tier, dte)
            assert isinstance(result, int), (
                f"Expected int, got {type(result)} for ({ask}, {vol}, {tier}, {dte})"
            )


# ===========================================================================
# CHUNK 4 — IngestionProcessor.enrich_tags()
# ===========================================================================

class TestEnrichTags:
    """
    enrich_tags() @staticmethod contract:
      1. Reads dte, premium, is_ask_side, vol_oi_signal from ev_dict.
      2. Computes dte_bucket, notional_tier, event_cipher_score.
      3. Mutates ev_dict in-place and returns the same object.
      4. Missing keys default to zero-score values (never raises).
    """

    # ---- basic wiring ----------------------------------------------------

    def test_returns_same_dict_object(self):
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        assert IngestionProcessor.enrich_tags(ev) is ev

    def test_mutates_dict_in_place(self):
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert "dte_bucket"         in ev
        assert "notional_tier"      in ev
        assert "event_cipher_score" in ev

    def test_three_keys_written_exactly(self):
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        before = set(ev.keys())
        IngestionProcessor.enrich_tags(ev)
        assert set(ev.keys()) - before == {"dte_bucket", "notional_tier", "event_cipher_score"}

    # ---- dte_bucket wiring ----------------------------------------------

    def test_dte_5_yields_5_60_bucket(self):
        ev = {"dte": 5, "premium": 10_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["dte_bucket"] == "5-60"

    def test_dte_0_yields_0dte_bucket(self):
        ev = {"dte": 0, "premium": 10_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["dte_bucket"] == "0DTE"

    def test_dte_none_yields_90plus_bucket(self):
        ev = {"dte": None, "premium": 10_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["dte_bucket"] == "90+"

    def test_dte_missing_key_yields_90plus_bucket(self):
        """Missing 'dte' key -> ev_dict.get('dte') returns None -> '90+'."""
        ev = {"premium": 10_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["dte_bucket"] == "90+"

    # ---- notional_tier wiring -------------------------------------------

    def test_premium_golden_yields_golden_tier(self):
        ev = {"dte": 5, "premium": 600_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "GOLDEN"

    def test_premium_block_yields_block_tier(self):
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "BLOCK"

    def test_premium_noteworthy_yields_noteworthy_tier(self):
        ev = {"dte": 5, "premium": 75_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "NOTEWORTHY"

    def test_premium_watch_yields_watch_tier(self):
        ev = {"dte": 5, "premium": 1_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "WATCH"

    def test_premium_none_yields_watch_tier(self):
        ev = {"dte": 5, "premium": None, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "WATCH"

    def test_premium_missing_key_yields_watch_tier(self):
        """Missing 'premium' key -> ev_dict.get('premium') returns None -> WATCH."""
        ev = {"dte": 5, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "WATCH"

    # ---- event_cipher_score wiring --------------------------------------

    def test_score_4_all_dims_fire(self):
        """Dim 1: ask_side=True, Dim 2: HIGH, Dim 3: GOLDEN, Dim 4: dte=3 -> '1-4'."""
        ev = {"dte": 3, "premium": 600_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 4

    def test_score_0_no_dims_fire(self):
        ev = {"dte": 0, "premium": 1_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 0

    def test_score_2_ask_and_vol(self):
        """0DTE + WATCH = 0 from Dims 3/4; ask_side + HIGH = 2 from Dims 1/2."""
        ev = {"dte": 0, "premium": 1_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 2

    def test_score_uses_computed_notional_tier_not_raw_premium(self):
        """Score is derived from notional_tier, not directly from the raw float."""
        ev = {"dte": 0, "premium": 100_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "BLOCK"       # BLOCK -> Dim 3 fires
        assert ev["event_cipher_score"] == 1

    def test_score_uses_computed_dte_bucket_not_raw_dte(self):
        """Score is derived from dte_bucket, not the raw int."""
        ev = {"dte": 2, "premium": 1_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["dte_bucket"] == "1-4"            # 1-4 -> Dim 4 fires
        assert ev["event_cipher_score"] == 1

    # ---- None-safety / missing-key robustness ---------------------------

    def test_empty_dict_never_raises(self):
        """enrich_tags must not raise on a completely empty input dict."""
        ev = {}
        IngestionProcessor.enrich_tags(ev)          # must not raise
        assert "dte_bucket"         in ev
        assert "notional_tier"      in ev
        assert "event_cipher_score" in ev

    def test_empty_dict_defaults_to_zero_score(self):
        ev = {}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 0

    def test_is_ask_side_missing_defaults_false(self):
        """Missing is_ask_side -> .get() default False -> Dim 1 doesn't fire."""
        ev = {"dte": 3, "premium": 600_000.0, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 3        # Dims 2/3/4 fire; Dim 1 doesn't

    def test_vol_oi_signal_missing_defaults_unknown(self):
        """Missing vol_oi_signal -> .get() default 'UNKNOWN' -> Dim 2 doesn't fire."""
        ev = {"dte": 3, "premium": 600_000.0, "is_ask_side": True}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 3        # Dims 1/3/4 fire; Dim 2 doesn't

    # ---- idempotency ----------------------------------------------------

    def test_idempotent_double_call(self):
        """Calling enrich_tags twice overwrites the same keys; values are stable."""
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        s1, b1, t1 = ev["event_cipher_score"], ev["dte_bucket"], ev["notional_tier"]

        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == s1
        assert ev["dte_bucket"]         == b1
        assert ev["notional_tier"]      == t1

    # ---- callable as static and as instance method ----------------------

    def test_callable_as_static_without_instance(self):
        """@staticmethod: callable on the class directly, no self required."""
        ev = {"dte": 10, "premium": 50_000.0, "is_ask_side": True, "vol_oi_signal": "NORMAL"}
        result = IngestionProcessor.enrich_tags(ev)
        assert result is ev
        assert ev["notional_tier"]      == "NOTEWORTHY"
        assert ev["dte_bucket"]         == "5-60"
        # Dim 1 (ask_side=True) + Dim 4 (5-60) = 2; NOTEWORTHY doesn't score Dim 3
        assert ev["event_cipher_score"] == 2

    def test_callable_on_instance_too(self):
        """Sanity: also works when called on an IngestionProcessor instance."""
        processor = IngestionProcessor()
        ev = {"dte": 3, "premium": 600_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        processor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 4
