"""
REARCH-005: Signal Config Store — test suite

Covers five QA contracts:

  R-1 — _cast() type-coercion fidelity:
    C-1  float: "1500000.0" → 1_500_000.0
    C-2  int:   "5"         → 5
    C-3  bool:  "true", "1", "yes", "on" → True; "false", "0" → False
    C-4  unknown type_string → raw string passthrough (no exception)
    C-5  unparseable float string → raw string passthrough (no TypeError/ValueError)

  R-2 — get_param() hot-path accessor:
    P-1  known key returns typed value from current snapshot (no DB call)
    P-2  unknown key returns caller-supplied default, not None
    P-3  get_param() never touches the DB — _fetch_from_db must not be called

  R-3 — get_effective_premium_threshold() tier-aware multiplier logic:
    T-1  tier1: returns base unchanged (no multiplier applied)
    T-2  tier2: returns base * t2_mult  (GOLDEN, BLOCK, NOTEWORTHY)
    T-3  tier3: returns base * t3_mult
    T-4  unknown notional_tier: falls back to base (not None, not 0.0, no KeyError)
    T-5  unknown alert_level_key: returns 0.0 (key absent from snapshot + _DEFAULTS)

  R-4 — get_signal_config() TTL cache and atomic snapshot swap:
    S-1  cache hit: _async_fetch_from_db NOT called when snapshot is fresh (< 30s)
    S-2  force_refresh=True: _async_fetch_from_db called even when TTL has not expired
    S-3  DB error path: stale snapshot returned (no exception raised)
    S-4  DB error + empty snapshot: _DEFAULTS dict returned (no exception raised)
    S-5  successful fetch: snapshot atomically swapped and returned copy is correct

  R-5 — SIGNAL_CONFIG_TYPES completeness:
    K-1  all 16 expected keys are present in SIGNAL_CONFIG_TYPES
         (3 base + 6 tier-mults + 2 ask-side + 1 vol-oi + 2 dte + 1 trade + 1 score = 16)
         NOTE: The module docstring previously referenced 19 keys — this is a
         documentation error. 16 is the authoritative count per the REARCH-005
         spec. Resolve in the implementation spec before cutting the impl branch.
    K-2  all declared type_strings are one of {"float", "int", "bool"}
    K-3  every key in SIGNAL_CONFIG_TYPES has a corresponding entry in _DEFAULTS
    K-4  _DEFAULTS values are correctly typed relative to SIGNAL_CONFIG_TYPES
         (float keys → float, int keys → int, bool keys → bool)

Design notes:
  - All Supabase env vars are stripped at module import time so
    signal_config_store runs in no-DB mode by default.
  - _async_fetch_from_db is patched with AsyncMock for all DB-path tests
    (R-4 / S-1..S-5).  The module-level name get_signal_config is rebound
    to async_get_signal_config at import time, which calls
    async_reload_signal_config() → _async_fetch_from_db().  The sync
    _fetch_from_db is NOT on this code path and must NOT be patched for
    these tests.
  - _snapshot and _snapshot_ts are reset in setUp to prevent cross-test leakage.
  - asyncio.run() is used for all async calls (Python 3.10+ / 3.12 safe).
    This suite intentionally avoids pytest-asyncio; asyncio.run() is explicit
    and does not require event-loop fixture configuration. If pytest-asyncio is
    added to the project later, migrate to @pytest.mark.asyncio to avoid nested
    event-loop conflicts.
  - get_effective_premium_threshold() is synchronous (reads _snapshot directly)
    and does not require asyncio.run().
  - Implementation constraint: get_signal_config() MUST use time.monotonic()
    (not time.time()) for TTL comparison. S-1 sets _snapshot_ts = time.monotonic()
    to simulate a fresh cache; if the implementation uses time.time() the TTL
    logic diverges and S-1 will produce false negatives.
"""

import asyncio
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

# Strip Supabase env vars before importing the module so every test starts
# in no-DB mode and _SUPABASE_URL / _SUPABASE_KEY are both None.
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)

import services.signal_config_store as scs  # noqa: E402  (import after env teardown)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_snapshot(snapshot_override: dict | None = None) -> None:
    """
    Reset module-level snapshot state to prevent cross-test leakage.

    scs._snapshot is replaced with a fresh copy of _DEFAULTS (or a custom
    override dict) and scs._snapshot_ts is set to 0.0 so the TTL is
    treated as expired by default.
    """
    scs._snapshot = dict(snapshot_override or scs._DEFAULTS)
    scs._snapshot_ts = 0.0


def _make_db_rows(overrides: dict | None = None) -> list[dict]:
    """
    Build a synthetic list of signal_config DB rows mirroring _DEFAULTS.

    Each row has the shape returned by the real Supabase REST endpoint:
      {"key": ..., "value": str(...), "value_type": ...}

    `overrides` is a {key: raw_string_value} dict applied on top of the
    default-derived values (used by S-5 to inject a specific known value).
    """
    rows = []
    for key, default_value in scs._DEFAULTS.items():
        value_type = scs.SIGNAL_CONFIG_TYPES.get(key, "float")
        rows.append({
            "key":        key,
            "value":      str(default_value),
            "value_type": value_type,
        })
    if overrides:
        for key, raw_value in overrides.items():
            # update existing row or append a new one
            for row in rows:
                if row["key"] == key:
                    row["value"] = raw_value
                    break
            else:
                rows.append({
                    "key":        key,
                    "value":      raw_value,
                    "value_type": scs.SIGNAL_CONFIG_TYPES.get(key, "float"),
                })
    return rows


# ---------------------------------------------------------------------------
# R-1: _cast() type-coercion fidelity (C-1 through C-5)
# ---------------------------------------------------------------------------

class TestCastTypeCoercion:
    """
    R-1: _cast() must produce correctly-typed Python values from raw DB text.

    _cast() is an internal helper but it is the sole coercion point for every
    DB-sourced value that enters the snapshot.  Errors here silently corrupt
    every downstream config read for the lifetime of the process.
    """

    def test_c1_float_string_coerced_correctly(self):
        """
        C-1: "1500000.0" with value_type="float" → 1_500_000.0 (Python float).
        Guards against the value arriving as a str instead of a numeric.
        """
        result = scs._cast("1500000.0", "float")
        assert isinstance(result, float), (
            f"Expected float, got {type(result).__name__!r}: {result!r}"
        )
        assert result == pytest.approx(1_500_000.0), (
            f"Expected 1_500_000.0, got {result!r}"
        )

    def test_c2_int_string_coerced_correctly(self):
        """
        C-2: "5" with value_type="int" → 5 (Python int).
        sig.min_dte, sig.max_dte, sig.min_trade_count, sig.steamroom_score_floor
        are all int keys; this guards the DTE gate and score gate in REARCH-006.
        """
        result = scs._cast("5", "int")
        assert isinstance(result, int), (
            f"Expected int, got {type(result).__name__!r}: {result!r}"
        )
        assert result == 5, f"Expected 5, got {result!r}"

    def test_c3_bool_string_truthy_values(self):
        """
        C-3a: "true", "1", "yes", "on" must all cast to True.
        The DB may store booleans in any of these representations depending on
        how the admin writes them; all must be treated as enabled.
        """
        for raw in ("true", "1", "yes", "on", "True", "TRUE"):
            result = scs._cast(raw, "bool")
            assert result is True, (
                f"Expected True for raw={raw!r}, got {result!r}"
            )

    def test_c3_bool_string_falsy_values(self):
        """
        C-3b: "false", "0" must cast to False.
        sig.require_ask_side and sig.require_vol_gt_oi are gating bools;
        a mis-cast True here would open a gate that should be closed.
        """
        for raw in ("false", "0", "False", "FALSE"):
            result = scs._cast(raw, "bool")
            assert result is False, (
                f"Expected False for raw={raw!r}, got {result!r}"
            )

    def test_c4_unknown_type_string_returns_raw_value(self):
        """
        C-4: An unrecognised value_type string must return the raw value
        unchanged, not raise an exception.
        Future ad-hoc keys with custom type labels must not crash _cast().
        """
        result = scs._cast("some_value", "custom_type")
        assert result == "some_value", (
            f"Expected passthrough 'some_value', got {result!r}"
        )

    def test_c5_unparseable_value_returns_raw_string(self):
        """
        C-5: A string that cannot be parsed as float/int (e.g. "not_a_number")
        with a numeric type_string must return the raw string without raising
        TypeError or ValueError.  This prevents a single corrupt DB row from
        crashing the entire config load.
        """
        try:
            result = scs._cast("not_a_number", "float")
        except (TypeError, ValueError) as exc:
            pytest.fail(
                f"_cast() raised {type(exc).__name__} on unparseable value: {exc}"
            )
        assert result == "not_a_number", (
            f"Expected raw passthrough 'not_a_number', got {result!r}"
        )


# ---------------------------------------------------------------------------
# R-2: get_param() hot-path accessor (P-1 through P-3)
# ---------------------------------------------------------------------------

class TestGetParam:
    """
    R-2: get_param() is the function REARCH-006 calls on every episode
    evaluation.  It must be fast (no DB), correct, and safe on missing keys.
    """

    def setup_method(self):
        _reset_snapshot()

    def test_p1_known_key_returns_typed_value_from_snapshot(self):
        """
        P-1: get_param("sig.min_dte") returns the int value currently in
        _snapshot (defaults to 5).  Value must be int, not a str or float.
        """
        scs._snapshot["sig.min_dte"] = 5
        result = scs.get_param("sig.min_dte", default=99)
        assert result == 5, f"Expected 5, got {result!r}"
        assert isinstance(result, int), (
            f"Expected int from snapshot, got {type(result).__name__!r}"
        )

    def test_p2_unknown_key_returns_caller_default(self):
        """
        P-2: get_param with an unrecognised key returns the caller-supplied
        default, never None.  REARCH-006 passes typed literals as defaults
        so that a missing config row never produces a None that would crash
        a comparison or arithmetic operation downstream.
        """
        result = scs.get_param("sig.nonexistent_key", default=42)
        assert result == 42, (
            f"Expected caller default 42 for missing key, got {result!r}"
        )
        assert result is not None, "get_param must never return None when a default is supplied"

    def test_p3_get_param_never_calls_fetch_from_db(self):
        """
        P-3: get_param() reads only from _snapshot.  _fetch_from_db must
        never be called — it is an async I/O operation and calling it on the
        hot path would block/delay every episode evaluation.
        """
        with patch(
            "services.signal_config_store._fetch_from_db",
            new=AsyncMock(side_effect=AssertionError("_fetch_from_db must not be called from get_param()")),
        ):
            result = scs.get_param("sig.ask_side_pct_floor", default=0.6)
        # If _fetch_from_db was called, the AsyncMock raises AssertionError before we get here.
        assert result is not None, "get_param returned None unexpectedly"


# ---------------------------------------------------------------------------
# R-3: get_effective_premium_threshold() tier-aware multiplier logic (T-1..T-5)
# ---------------------------------------------------------------------------

class TestGetEffectivePremiumThreshold:
    """
    R-3: get_effective_premium_threshold() is the Dimension-1 entry point for
    REARCH-006.  It must return base * multiplier for Tier-2/3 and base alone
    for Tier-1.  The multiplier key lookup uses _TIER_MULT_KEYS; errors here
    silently disable premium filtering for one or more tiers.
    """

    def setup_method(self):
        _reset_snapshot()

    def test_t1_tier1_returns_base_unchanged(self):
        """
        T-1: Tier-1 has no multiplier (None in _TIER_MULT_KEYS).
        get_effective_premium_threshold("sig.golden_sweep_premium", "tier1")
        must return exactly the base threshold: 1_000_000.0.
        """
        result = scs.get_effective_premium_threshold("sig.golden_sweep_premium", "tier1")
        assert result == pytest.approx(1_000_000.0), (
            f"Expected 1_000_000.0 for GOLDEN tier1, got {result!r}"
        )

    def test_t2_tier2_golden_applies_half_multiplier(self):
        """
        T-2a: GOLDEN + tier2 → base * 0.5 = 500_000.0.
        Effective threshold: $500k is correct at Steamroom defaults.
        """
        result = scs.get_effective_premium_threshold("sig.golden_sweep_premium", "tier2")
        assert result == pytest.approx(500_000.0, rel=1e-6), (
            f"Expected 500_000.0 (1_000_000 * 0.5) for GOLDEN tier2, got {result!r}"
        )

    def test_t2_tier2_block_applies_half_multiplier(self):
        """
        T-2b: BLOCK + tier2 → 500_000.0 * 0.5 = 250_000.0.
        """
        result = scs.get_effective_premium_threshold("sig.block_premium", "tier2")
        assert result == pytest.approx(250_000.0, rel=1e-6), (
            f"Expected 250_000.0 (500_000 * 0.5) for BLOCK tier2, got {result!r}"
        )

    def test_t2_tier2_noteworthy_applies_half_multiplier(self):
        """
        T-2c: NOTEWORTHY + tier2 → 50_000.0 * 0.5 = 25_000.0.
        """
        result = scs.get_effective_premium_threshold("sig.noteworthy_premium", "tier2")
        assert result == pytest.approx(25_000.0, rel=1e-6), (
            f"Expected 25_000.0 (50_000 * 0.5) for NOTEWORTHY tier2, got {result!r}"
        )

    def test_t3_tier3_golden_applies_point2_multiplier(self):
        """
        T-3a: GOLDEN + tier3 → 1_000_000.0 * 0.2 = 200_000.0.
        """
        result = scs.get_effective_premium_threshold("sig.golden_sweep_premium", "tier3")
        assert result == pytest.approx(200_000.0, rel=1e-6), (
            f"Expected 200_000.0 (1_000_000 * 0.2) for GOLDEN tier3, got {result!r}"
        )

    def test_t3_tier3_block_applies_point2_multiplier(self):
        """
        T-3b: BLOCK + tier3 → 500_000.0 * 0.2 = 100_000.0.
        """
        result = scs.get_effective_premium_threshold("sig.block_premium", "tier3")
        assert result == pytest.approx(100_000.0, rel=1e-6), (
            f"Expected 100_000.0 (500_000 * 0.2) for BLOCK tier3, got {result!r}"
        )

    def test_t3_tier3_noteworthy_applies_point2_multiplier(self):
        """
        T-3c: NOTEWORTHY + tier3 → 50_000.0 * 0.2 = 10_000.0.
        """
        result = scs.get_effective_premium_threshold("sig.noteworthy_premium", "tier3")
        assert result == pytest.approx(10_000.0, rel=1e-6), (
            f"Expected 10_000.0 (50_000 * 0.2) for NOTEWORTHY tier3, got {result!r}"
        )

    def test_t4_unknown_notional_tier_falls_back_to_base(self):
        """
        T-4: An unrecognised notional_tier (e.g. "tier4", "UNKNOWN") must
        return the base threshold unchanged — not None, not 0.0, no KeyError.

        REARCH-006 reads notional_tier from flow_episodes rows written by
        REARCH-004.  If a new tier value is introduced later, the signal
        engine must not silently pass or block all episodes; it must degrade
        gracefully to the base Tier-1 threshold.

        Three assertions — weakest to strongest:
          1. result is not None  — a swallowed KeyError returning None would
             cause TypeError on the approx comparison below, masking the bug.
          2. result > 0.0        — 0.0 would disable the premium gate entirely.
          3. result ≈ 1_000_000  — must be the Tier-1 base, not an arbitrary value.
        """
        result = scs.get_effective_premium_threshold("sig.golden_sweep_premium", "tier99")
        assert result is not None, (
            "Unknown tier returned None — likely a swallowed KeyError. "
            "Must fall back to base threshold, not None."
        )
        assert result > 0.0, (
            "Unknown tier must not return 0.0 — would disable the premium gate entirely"
        )
        assert result == pytest.approx(1_000_000.0), (
            f"Expected base 1_000_000.0 for unknown tier, got {result!r}"
        )

    def test_t5_unknown_alert_level_key_returns_zero(self):
        """
        T-5: An unrecognised alert_level_key (not in snapshot or _DEFAULTS)
        returns 0.0 — the float default when both .get() calls miss.

        This is documented behaviour: callers must only pass one of the three
        canonical keys.  A zero return is distinguishable and REARCH-006 can
        treat it as a config error.
        """
        result = scs.get_effective_premium_threshold("sig.nonexistent_alert", "tier1")
        assert result == 0.0, (
            f"Expected 0.0 for unknown alert_level_key, got {result!r}"
        )


# ---------------------------------------------------------------------------
# R-4: get_signal_config() TTL cache and atomic snapshot swap (S-1..S-5)
# ---------------------------------------------------------------------------

class TestGetSignalConfigCache:
    """
    R-4: get_signal_config() must respect a 30-second TTL, support
    force_refresh=True, and never raise on DB errors.

    All DB calls are patched so no Supabase URL is required.

    IMPORTANT — correct patch target:
      The module-level name `get_signal_config` is rebound to
      `async_get_signal_config` at import time.  That function calls
      `async_reload_signal_config()` which calls `_async_fetch_from_db()`.
      Tests must therefore patch `_async_fetch_from_db`, NOT the sync
      `_fetch_from_db`.  Patching `_fetch_from_db` here has no effect on
      the async code path and will silently produce wrong results.
    """

    def setup_method(self):
        _reset_snapshot()

    def test_s1_cache_hit_does_not_call_fetch_from_db(self):
        """
        S-1: When the snapshot was loaded less than _CACHE_TTL seconds ago,
        get_signal_config() must return the cached snapshot without hitting the DB.

        A fresh _snapshot_ts is simulated by setting it to time.monotonic()
        (i.e. "just refreshed").  _async_fetch_from_db must not be called.

        Implementation constraint: get_signal_config() MUST use time.monotonic()
        for its TTL comparison — not time.time(). This test sets _snapshot_ts
        via time.monotonic(); if the implementation compares against time.time()
        the TTL will always appear expired and this test will produce a false
        negative (fetch called when it shouldn't be).
        """
        scs._snapshot = dict(scs._DEFAULTS)
        scs._snapshot_ts = time.monotonic()  # just refreshed — TTL not expired

        fetch_mock = AsyncMock(return_value=None)
        with patch("services.signal_config_store._async_fetch_from_db", new=fetch_mock):
            result = asyncio.run(scs.get_signal_config())

        fetch_mock.assert_not_called()
        assert isinstance(result, dict), "get_signal_config must return a dict"
        assert "sig.min_dte" in result, "Cached snapshot must contain expected keys"

    def test_s2_force_refresh_calls_fetch_from_db_despite_fresh_ttl(self):
        """
        S-2: force_refresh=True must bypass the TTL and call _async_fetch_from_db
        even when the snapshot was loaded less than 30 seconds ago.

        Called from reload_signal_config() (admin PATCH endpoint, REARCH-008)
        so operators see their writes reflected immediately rather than waiting
        up to 30 seconds for the TTL to expire.
        """
        scs._snapshot_ts = time.monotonic()  # TTL still fresh
        scs._snapshot = dict(scs._DEFAULTS)

        refreshed_snapshot = dict(scs._DEFAULTS)
        refreshed_snapshot["sig.min_dte"] = 7  # operator just changed this

        fetch_mock = AsyncMock(return_value=refreshed_snapshot)
        with patch("services.signal_config_store._async_fetch_from_db", new=fetch_mock):
            result = asyncio.run(scs.get_signal_config(force_refresh=True))

        fetch_mock.assert_called_once()
        assert result.get("sig.min_dte") == 7, (
            f"Expected forced-refresh value 7, got {result.get('sig.min_dte')!r}"
        )

    def test_s3_db_error_returns_stale_snapshot_without_raising(self):
        """
        S-3: When _async_fetch_from_db returns None (network error / non-200 HTTP),
        get_signal_config() must return the current stale snapshot and must
        NOT raise an exception.

        The Signal Engine calls get_signal_config() on every episode
        evaluation; a transient DB error must never crash the engine or
        suppress episode signals.
        """
        scs._snapshot = dict(scs._DEFAULTS)
        scs._snapshot["sig.min_dte"] = 5
        scs._snapshot_ts = 0.0  # TTL expired — forces a fetch attempt

        fetch_mock = AsyncMock(return_value=None)  # simulates DB error
        with patch("services.signal_config_store._async_fetch_from_db", new=fetch_mock):
            try:
                result = asyncio.run(scs.get_signal_config())
            except Exception as exc:
                pytest.fail(
                    f"get_signal_config() raised {type(exc).__name__} on DB error: {exc}"
                )

        assert isinstance(result, dict), "Must return a dict even on DB error"
        assert result.get("sig.min_dte") == 5, (
            f"Stale snapshot value must be returned on DB error, "
            f"got sig.min_dte={result.get('sig.min_dte')!r}"
        )

    def test_s4_db_error_with_empty_snapshot_returns_defaults(self):
        """
        S-4: When _async_fetch_from_db returns None AND _snapshot is empty
        (cold-start with no prior successful fetch), get_signal_config() must
        return _DEFAULTS without raising.

        This is the bootstrapping edge case: the very first call on process
        start if Supabase is unreachable.  The Signal Engine must still have
        a valid config rather than receiving an empty dict.
        """
        scs._snapshot = {}       # empty — simulates no prior successful load
        scs._snapshot_ts = 0.0

        fetch_mock = AsyncMock(return_value=None)
        with patch("services.signal_config_store._async_fetch_from_db", new=fetch_mock):
            try:
                result = asyncio.run(scs.get_signal_config())
            except Exception as exc:
                pytest.fail(
                    f"get_signal_config() raised {type(exc).__name__} on empty-snapshot + DB error: {exc}"
                )

        assert isinstance(result, dict), "Must return a dict even with empty snapshot"
        assert len(result) > 0, "Fallback result must not be empty — _DEFAULTS should apply"
        assert "sig.min_dte" in result, (
            "Fallback to _DEFAULTS must include sig.min_dte"
        )

    def test_s5_successful_fetch_atomically_swaps_snapshot(self):
        """
        S-5: When _async_fetch_from_db returns a valid dict, get_signal_config() must:
          1. Replace _snapshot with the new dict (atomic swap)
          2. Update _snapshot_ts to the current monotonic time
          3. Return a COPY of the new snapshot (not the internal reference)

        An operator just changed sig.ask_side_pct_floor from 0.6 to 0.75.
        The returned config must reflect 0.75 and the internal _snapshot
        must also be updated (so subsequent get_param() calls see the new value).
        """
        scs._snapshot_ts = 0.0  # TTL expired — forces fetch

        updated_snapshot = dict(scs._DEFAULTS)
        updated_snapshot["sig.ask_side_pct_floor"] = 0.75

        fetch_mock = AsyncMock(return_value=updated_snapshot)
        before_ts = time.monotonic()
        with patch("services.signal_config_store._async_fetch_from_db", new=fetch_mock):
            result = asyncio.run(scs.get_signal_config())
        after_ts = time.monotonic()

        # Returned dict must reflect the new value
        assert result.get("sig.ask_side_pct_floor") == pytest.approx(0.75), (
            f"Expected 0.75 after snapshot swap, got {result.get('sig.ask_side_pct_floor')!r}"
        )

        # Internal _snapshot must also be updated (so get_param() is consistent)
        assert scs._snapshot.get("sig.ask_side_pct_floor") == pytest.approx(0.75), (
            f"Internal _snapshot not updated after atomic swap: "
            f"sig.ask_side_pct_floor={scs._snapshot.get('sig.ask_side_pct_floor')!r}"
        )

        # _snapshot_ts must be refreshed to a reasonable current time
        assert before_ts <= scs._snapshot_ts <= after_ts + 1.0, (
            f"_snapshot_ts not updated: {scs._snapshot_ts!r} not in [{before_ts:.3f}, {after_ts:.3f}]"
        )

        # Returned dict must be a copy — mutating it must not affect _snapshot
        result["sig.ask_side_pct_floor"] = 999.0
        assert scs._snapshot.get("sig.ask_side_pct_floor") == pytest.approx(0.75), (
            "get_signal_config() returned internal reference — must return a dict copy"
        )


# ---------------------------------------------------------------------------
# R-5: SIGNAL_CONFIG_TYPES completeness (K-1 through K-4)
# ---------------------------------------------------------------------------

class TestSignalConfigTypesCompleteness:
    """
    R-5: SIGNAL_CONFIG_TYPES is the authoritative registry used by _cast(),
    the admin PATCH endpoint (REARCH-008), and the backtest config-override
    type-safety layer (REARCH-014).  A missing key causes a DB row to be
    silently ignored on load; an incorrect type causes silent coercion errors.

    These tests are intentionally exhaustive — a passing R-5 suite is the
    contract that REARCH-006 and REARCH-008 can rely on.
    """

    # Authoritative key count: 16
    #   3 base premiums       (golden_sweep, block, noteworthy)
    #   6 tier multipliers    (t2 + t3 for each of the 3 base keys)
    #   2 ask-side            (require_ask_side, ask_side_pct_floor)
    #   1 vol-oi              (require_vol_gt_oi)
    #   2 dte                 (min_dte, max_dte)
    #   1 trade count         (min_trade_count)
    #   1 score floor         (steamroom_score_floor)
    #   ──────────────────────
    #   16 total
    #
    # NOTE: A prior version of the module docstring referenced 19 keys. That
    # was a documentation error. 16 is the correct count per the REARCH-005
    # spec deliberation. The implementation spec must declare 16 as canonical
    # before the impl branch is cut — K-1 will immediately fail if the module
    # ships with a different count.
    _EXPECTED_KEYS: frozenset[str] = frozenset({
        # Dimension 1 — base premiums (Tier-1 thresholds)
        "sig.golden_sweep_premium",
        "sig.block_premium",
        "sig.noteworthy_premium",
        # Dimension 1 — tier multipliers
        "sig.golden_sweep_premium_t2_mult",
        "sig.golden_sweep_premium_t3_mult",
        "sig.block_premium_t2_mult",
        "sig.block_premium_t3_mult",
        "sig.noteworthy_premium_t2_mult",
        "sig.noteworthy_premium_t3_mult",
        # Dimension 2 — ask-side quality gates
        "sig.require_ask_side",
        "sig.ask_side_pct_floor",
        # Dimension 3 — vol/OI quality gate
        "sig.require_vol_gt_oi",
        # Dimension 4 — DTE window
        "sig.min_dte",
        "sig.max_dte",
        # Dimension 5 — trade count floor
        "sig.min_trade_count",
        # Scoring — Steamroom score gate
        "sig.steamroom_score_floor",
    })

    def test_k1_all_expected_keys_present_in_signal_config_types(self):
        """
        K-1: Every key enumerated in _EXPECTED_KEYS (16 total) must appear in
        SIGNAL_CONFIG_TYPES.  A missing key means _cast() will fall back to
        the row's own value_type column (which may differ or be absent) and
        the admin PATCH endpoint will reject writes for that key as "unknown".
        """
        declared = frozenset(scs.SIGNAL_CONFIG_TYPES.keys())
        missing = self._EXPECTED_KEYS - declared
        assert not missing, (
            f"Keys missing from SIGNAL_CONFIG_TYPES: {sorted(missing)}\n"
            f"These keys are required by REARCH-006 / REARCH-008 but will be "
            f"treated as unknown, causing silent DB-read misses and 422 write rejections."
        )

    def test_k2_all_declared_type_strings_are_valid(self):
        """
        K-2: Every value in SIGNAL_CONFIG_TYPES must be one of
        {"float", "int", "bool"}.  An invalid type_string causes _cast()
        to return the raw DB string instead of a typed Python value, which
        will crash any arithmetic or boolean comparison downstream.
        """
        valid_types = {"float", "int", "bool"}
        invalid = {
            key: type_str
            for key, type_str in scs.SIGNAL_CONFIG_TYPES.items()
            if type_str not in valid_types
        }
        assert not invalid, (
            f"Invalid type_string values in SIGNAL_CONFIG_TYPES:\n"
            + "\n".join(f"  {k!r}: {v!r}" for k, v in sorted(invalid.items()))
            + f"\nValid values are: {valid_types}"
        )

    def test_k3_every_signal_config_types_key_has_a_default(self):
        """
        K-3: Every key declared in SIGNAL_CONFIG_TYPES must have a
        corresponding entry in _DEFAULTS.

        _DEFAULTS is the fallback snapshot used when Supabase is unreachable.
        A key present in SIGNAL_CONFIG_TYPES but absent from _DEFAULTS means
        get_param(key, default=None) returns None from the fallback path —
        which crashes arithmetic in REARCH-006 with no clear error.
        """
        declared_keys = frozenset(scs.SIGNAL_CONFIG_TYPES.keys())
        defaults_keys = frozenset(scs._DEFAULTS.keys())
        missing_defaults = declared_keys - defaults_keys
        assert not missing_defaults, (
            f"Keys in SIGNAL_CONFIG_TYPES with no _DEFAULTS entry: {sorted(missing_defaults)}\n"
            f"Add these keys to _DEFAULTS so the fallback path never returns None."
        )

    def test_k4_defaults_are_correctly_typed_relative_to_signal_config_types(self):
        """
        K-4: For every key in SIGNAL_CONFIG_TYPES, the _DEFAULTS[key] value
        must be an instance of the declared Python type:
          "float" → float
          "int"   → int
          "bool"  → bool

        A type mismatch here means _DEFAULTS and the DB-loaded values diverge
        in Python type, which can cause subtle comparison bugs when the Signal
        Engine compares a DB-sourced int with a default float, or vice versa.
        """
        type_map = {"float": float, "int": int, "bool": bool}
        mismatches: list[str] = []

        for key, type_str in scs.SIGNAL_CONFIG_TYPES.items():
            expected_type = type_map.get(type_str)
            if expected_type is None:
                continue  # K-2 guards invalid type strings; skip here

            default_value = scs._DEFAULTS.get(key)
            if default_value is None:
                continue  # K-3 guards missing defaults; skip here

            if not isinstance(default_value, expected_type):
                mismatches.append(
                    f"  {key!r}: declared={type_str!r}, "
                    f"default type={type(default_value).__name__!r}, "
                    f"value={default_value!r}"
                )

        assert not mismatches, (
            f"Type mismatches between SIGNAL_CONFIG_TYPES and _DEFAULTS:\n"
            + "\n".join(mismatches)
        )
