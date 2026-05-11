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
  - DTE bucket names match processor.py constants exactly (0DTE, 1-4, 5-60,
    61-90, 90+).
  - Notional tiers match processor.py constants (GOLDEN, BLOCK, NOTEWORTHY,
    WATCH).
  - Score range is 0-4 (Dim 5 is REARCH-004 scope).

All tests run offline — no Supabase or Tradier connectivity required.
"""
import sys
import types
import importlib
import pytest

# ---------------------------------------------------------------------------
# Bootstrap: import processor without triggering heavy side-effect imports
# (supabase_client, chain_store, etc.) that are not available in CI without
# env vars. We stub only the external modules processor.py imports at the
# top level; the functions under test are pure and never reach those stubs.
# ---------------------------------------------------------------------------

def _stub_module(name: str) -> types.ModuleType:
    """Register a minimal stub in sys.modules so `import name` succeeds."""
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod


for _m in [
    "supabase",
    "postgrest",
    "services.supabase_client",
    "services.chain_store",
    "services.gate_config_store",
    "ingestion.symbol_registry",
    "core.event_bus",
]:
    _stub_module(_m)

# Make ingestion a proper package stub so relative imports work
_ing_pkg = _stub_module("ingestion")
_ing_pkg.__path__ = []
_ing_pkg.__package__ = "ingestion"

import os
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")

from ingestion.processor import (
    _compute_dte_bucket,
    _compute_notional_tier,
    _compute_cipher_score,
    IngestionProcessor,
)


# ===========================================================================
# _compute_dte_bucket
# ===========================================================================

class TestComputeDteBucket:
    """
    Boundary table:
      dte=None  -> '90+'
      dte < 0   -> '90+'
      dte = 0   -> '0DTE'
      dte = 1   -> '1-4'
      dte = 4   -> '1-4'
      dte = 5   -> '5-60'
      dte = 60  -> '5-60'
      dte = 61  -> '61-90'
      dte = 90  -> '61-90'
      dte = 91  -> '90+'
      dte = 365 -> '90+'
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
            assert isinstance(_compute_dte_bucket(dte), str)


# ===========================================================================
# _compute_notional_tier
# ===========================================================================

class TestComputeNotionalTier:
    """
    Threshold table (inclusive lower bounds from processor.py constants):
      >= 500_000  -> 'GOLDEN'
      >= 100_000  -> 'BLOCK'
      >= 50_000   -> 'NOTEWORTHY'
      < 50_000    -> 'WATCH'
      None / 0.0  -> 'WATCH'
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
            assert isinstance(_compute_notional_tier(p), str)


# ===========================================================================
# _compute_cipher_score
# ===========================================================================

class TestComputeCipherScore:
    """
    Four binary dimensions, each worth 1 point. Range: 0-4.

    Dim 1 — is_ask_side == True
    Dim 2 — vol_oi_signal == 'HIGH'
    Dim 3 — notional_tier in ('BLOCK', 'GOLDEN')
    Dim 4 — dte_bucket in ('1-4', '5-60')
    """

    # ---- zero-score cases ------------------------------------------------

    def test_all_zero_score(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="WATCH",
            dte_bucket="0DTE",
        )
        assert score == 0

    def test_zero_score_leaps(self):
        """LEAPS (90+) bucket does not score Dim 4."""
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="UNKNOWN",
            notional_tier="NOTEWORTHY",
            dte_bucket="90+",
        )
        assert score == 0

    # ---- each dimension individually -------------------------------------

    def test_dim1_ask_side_adds_1(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal="NORMAL",
            notional_tier="WATCH",
            dte_bucket="90+",
        )
        assert score == 1

    def test_dim2_vol_oi_high_adds_1(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="HIGH",
            notional_tier="WATCH",
            dte_bucket="90+",
        )
        assert score == 1

    def test_dim3_block_adds_1(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="BLOCK",
            dte_bucket="90+",
        )
        assert score == 1

    def test_dim3_golden_adds_1(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="GOLDEN",
            dte_bucket="90+",
        )
        assert score == 1

    def test_dim4_1_4_adds_1(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="WATCH",
            dte_bucket="1-4",
        )
        assert score == 1

    def test_dim4_5_60_adds_1(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="WATCH",
            dte_bucket="5-60",
        )
        assert score == 1

    # ---- dim 4 non-scoring buckets ---------------------------------------

    def test_dim4_0dte_does_not_score(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="WATCH",
            dte_bucket="0DTE",
        )
        assert score == 0

    def test_dim4_61_90_does_not_score(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="WATCH",
            dte_bucket="61-90",
        )
        assert score == 0

    # ---- dim 3 non-scoring tiers ----------------------------------------

    def test_dim3_noteworthy_does_not_score(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="NOTEWORTHY",
            dte_bucket="90+",
        )
        assert score == 0

    def test_dim3_watch_does_not_score(self):
        score = _compute_cipher_score(
            is_ask_side=False,
            vol_oi_signal="NORMAL",
            notional_tier="WATCH",
            dte_bucket="90+",
        )
        assert score == 0

    # ---- max score -------------------------------------------------------

    def test_max_score_4(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal="HIGH",
            notional_tier="GOLDEN",
            dte_bucket="1-4",
        )
        assert score == 4

    def test_max_score_4_with_block_and_5_60(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal="HIGH",
            notional_tier="BLOCK",
            dte_bucket="5-60",
        )
        assert score == 4

    # ---- partial scores --------------------------------------------------

    def test_score_2_ask_side_plus_vol_oi(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal="HIGH",
            notional_tier="WATCH",
            dte_bucket="90+",
        )
        assert score == 2

    def test_score_3_missing_only_dte(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal="HIGH",
            notional_tier="GOLDEN",
            dte_bucket="61-90",
        )
        assert score == 3

    # ---- None-safety (never raises) --------------------------------------

    def test_none_is_ask_side_treated_as_false(self):
        score = _compute_cipher_score(
            is_ask_side=None,
            vol_oi_signal="HIGH",
            notional_tier="GOLDEN",
            dte_bucket="1-4",
        )
        # Dim 1 scores 0; Dims 2/3/4 score
        assert score == 3

    def test_none_vol_oi_signal_treated_as_unknown(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal=None,
            notional_tier="GOLDEN",
            dte_bucket="1-4",
        )
        # Dim 2 scores 0; Dims 1/3/4 score
        assert score == 3

    def test_none_notional_tier_treated_as_watch(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal="HIGH",
            notional_tier=None,
            dte_bucket="1-4",
        )
        # Dim 3 scores 0; Dims 1/2/4 score
        assert score == 3

    def test_none_dte_bucket_treated_as_90plus(self):
        score = _compute_cipher_score(
            is_ask_side=True,
            vol_oi_signal="HIGH",
            notional_tier="GOLDEN",
            dte_bucket=None,
        )
        # Dim 4 scores 0; Dims 1/2/3 score
        assert score == 3

    def test_all_none_inputs_returns_zero(self):
        score = _compute_cipher_score(
            is_ask_side=None,
            vol_oi_signal=None,
            notional_tier=None,
            dte_bucket=None,
        )
        assert score == 0

    def test_return_type_is_int(self):
        for (ask, vol, tier, dte) in [
            (True, "HIGH", "GOLDEN", "1-4"),
            (False, "NORMAL", "WATCH", "90+"),
            (None, None, None, None),
        ]:
            result = _compute_cipher_score(ask, vol, tier, dte)
            assert isinstance(result, int), f"Expected int, got {type(result)} for inputs ({ask}, {vol}, {tier}, {dte})"


# ===========================================================================
# IngestionProcessor.enrich_tags()
# ===========================================================================

class TestEnrichTags:
    """
    enrich_tags() is a staticmethod that:
      1. Reads dte, premium, is_ask_side, vol_oi_signal from ev_dict.
      2. Computes dte_bucket, notional_tier, event_cipher_score.
      3. Mutates ev_dict in-place with those three keys.
      4. Returns the same dict object.
    """

    # ---- basic wiring ----------------------------------------------------

    def test_returns_same_dict_object(self):
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        result = IngestionProcessor.enrich_tags(ev)
        assert result is ev

    def test_mutates_dict_in_place(self):
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert "dte_bucket" in ev
        assert "notional_tier" in ev
        assert "event_cipher_score" in ev

    def test_three_keys_written_exactly(self):
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        before_keys = set(ev.keys())
        IngestionProcessor.enrich_tags(ev)
        new_keys = set(ev.keys()) - before_keys
        assert new_keys == {"dte_bucket", "notional_tier", "event_cipher_score"}

    # ---- dte_bucket wiring -----------------------------------------------

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
        """Missing 'dte' key defaults to None -> '90+'"""
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
        ev = {"dte": 5, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "WATCH"

    # ---- event_cipher_score wiring --------------------------------------

    def test_score_4_all_dims_fire(self):
        """
        Dim 1: is_ask_side=True
        Dim 2: vol_oi_signal='HIGH'
        Dim 3: notional_tier='GOLDEN' (premium >= 500k)
        Dim 4: dte_bucket='1-4' (dte=3)
        """
        ev = {"dte": 3, "premium": 600_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 4

    def test_score_0_no_dims_fire(self):
        ev = {"dte": 0, "premium": 1_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 0

    def test_score_2_ask_and_vol(self):
        ev = {"dte": 0, "premium": 1_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 2

    def test_score_uses_computed_notional_tier_not_raw_premium(self):
        """
        The score must be computed from the derived notional_tier, not directly
        from the raw premium float. Ensures the internal call chain is correct.
        """
        # premium=100_000 -> BLOCK -> Dim 3 fires
        ev = {"dte": 0, "premium": 100_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["notional_tier"] == "BLOCK"
        assert ev["event_cipher_score"] == 1

    def test_score_uses_computed_dte_bucket_not_raw_dte(self):
        """
        The score must be computed from the derived dte_bucket, not the raw int.
        dte=2 -> '1-4' -> Dim 4 fires.
        """
        ev = {"dte": 2, "premium": 1_000.0, "is_ask_side": False, "vol_oi_signal": "NORMAL"}
        IngestionProcessor.enrich_tags(ev)
        assert ev["dte_bucket"] == "1-4"
        assert ev["event_cipher_score"] == 1

    # ---- None-safety / missing-key robustness ---------------------------

    def test_empty_dict_never_raises(self):
        """enrich_tags must not raise even with a completely empty input dict."""
        ev = {}
        IngestionProcessor.enrich_tags(ev)  # should not raise
        assert "dte_bucket" in ev
        assert "notional_tier" in ev
        assert "event_cipher_score" in ev

    def test_empty_dict_defaults_to_zero_score(self):
        ev = {}
        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 0

    def test_is_ask_side_missing_defaults_false(self):
        """Missing is_ask_side key -> Dim 1 does not fire."""
        ev = {"dte": 3, "premium": 600_000.0, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        # Dims 2, 3, 4 fire; Dim 1 doesn't -> score 3
        assert ev["event_cipher_score"] == 3

    def test_vol_oi_signal_missing_defaults_unknown(self):
        """Missing vol_oi_signal key -> Dim 2 does not fire."""
        ev = {"dte": 3, "premium": 600_000.0, "is_ask_side": True}
        IngestionProcessor.enrich_tags(ev)
        # Dims 1, 3, 4 fire; Dim 2 doesn't -> score 3
        assert ev["event_cipher_score"] == 3

    # ---- round-trip: calling enrich_tags twice is idempotent ------------

    def test_idempotent_double_call(self):
        """
        Calling enrich_tags twice on the same dict must yield the same result.
        The three output keys get overwritten, not accumulated.
        """
        ev = {"dte": 5, "premium": 200_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        IngestionProcessor.enrich_tags(ev)
        first_score = ev["event_cipher_score"]
        first_bucket = ev["dte_bucket"]
        first_tier = ev["notional_tier"]

        IngestionProcessor.enrich_tags(ev)
        assert ev["event_cipher_score"] == first_score
        assert ev["dte_bucket"] == first_bucket
        assert ev["notional_tier"] == first_tier

    # ---- static method callable without instance ------------------------

    def test_callable_as_static_without_instance(self):
        """enrich_tags is @staticmethod and must not require self."""
        ev = {"dte": 10, "premium": 50_000.0, "is_ask_side": True, "vol_oi_signal": "NORMAL"}
        # Call on the class, not an instance
        result = IngestionProcessor.enrich_tags(ev)
        assert result is ev
        assert ev["notional_tier"] == "NOTEWORTHY"
        assert ev["dte_bucket"] == "5-60"
        # Dim 1 (is_ask_side=True) + Dim 4 (5-60) = 2; Dim 3 (NOTEWORTHY doesn't score)
        assert ev["event_cipher_score"] == 2

    def test_callable_on_instance_too(self):
        """Sanity: also works when called via an instance."""
        processor = IngestionProcessor()
        ev = {"dte": 3, "premium": 600_000.0, "is_ask_side": True, "vol_oi_signal": "HIGH"}
        processor.enrich_tags(ev)
        assert ev["event_cipher_score"] == 4
