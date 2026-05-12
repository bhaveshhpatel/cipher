"""
REARCH-004: Episode Quality Enrichment — test suite

Covers three QA contracts:

  QA-1 — INSERT path seeds all four aggregate columns correctly:
    E-1  ask-side seed event → ask_side_count=1, ask_side_pct=1.0
    E-2  bid-side seed event → ask_side_count=0, ask_side_pct=0.0
    E-3  dte_bucket written to INSERT payload from signal_data
    E-4  notional_tier written to INSERT payload from signal_data
    E-5  missing is_ask_side key → treated as False (ask_side_count=0)
    E-6  created_episodes counter increments on successful INSERT

  QA-2 — PATCH paths never touch dte_bucket / notional_tier (SA-3 seed-only):
    E-7  in-flight PATCH payload excludes dte_bucket and notional_tier
    E-7b DB-lookup PATCH payload excludes dte_bucket and notional_tier
         (SA-3 deliberation finding — second distinct code branch)

  QA-3 — ask_side_pct precision contract (NUMERIC(5,4)):
    E-8  ask_side_pct rounded to 4 decimal places

Design notes:
  - All tests drive persist_flow_episode() directly with a signal_data dict.
  - _insert_rows_with_episode_id() and _lookup_open_episode() are patched so
    no real HTTP calls are issued (no SUPABASE_URL required).
  - The in-flight cache (_episode_in_flight) is cleared in setUp / each test
    to prevent cross-test state leakage.
  - asyncio.run() is used for all async calls (Python 3.10+ / 3.12 safe).

PBE-1 NULL contract: pre-REARCH rows have NULL ask_side_count; the DB-lookup
PATCH path uses COALESCE(NULL, 0) before incrementing. This is tested directly
in E-7b: the existing row returned by _lookup_open_episode has ask_side_count=None
and the resulting ask_side_pct must still be computed correctly.

FIX (REARCH-004 2026-05-12):
  Bug A — fs._stats does not exist; the module dict is fs._episode_stats.
    All references to fs._stats[...] replaced with fs._episode_stats[...].

  Bug B — _fake_insert() in _run_episode() had wrong signature.
    persist_flow_episode() calls:
      _insert_rows_with_episode_id(table, row, key, premium, current_oi, ask_side_count=N)
    The old fake accepted (payload, merge_key) which never matched, causing
    the patch to be a no-op and captured{} to stay empty.
    Fix: match the real signature (table, row, key, premium, current_oi=None, **kwargs)
    and capture `row` (the second positional arg = the insert payload dict).

  Bug C — _run_episode_patch() built the merge key with ":" separators.
    flow_store._episode_key() uses "|" separators:
      f"{ticker}|{direction}|{contract_type}|{strike}|{expiry}"
    The mismatch meant _episode_in_flight was seeded under a key that
    persist_flow_episode() never looked up, so in_flight was always None
    and the test took the INSERT path instead of the PATCH path.
    Fix: mirror the exact _episode_key() format using "|" separators.

  Bug D (2026-05-12) — E-3 / E-4: stale pre-deliberation bucket/tier names.
    production ignores signal_data['dte_bucket'] / signal_data['notional_tier']
    and recomputes from signal_data['dte'] and signal_data['premium'] via
    _compute_dte_bucket() / _compute_notional_tier().

    E-3 was asserting 'dte_bucket' == '0-7d' — not a valid _compute_dte_bucket()
    output.  Without a 'dte' key, dte=None → '90+'.
    Fix: add dte=3 to _make_signal_data; E-3 passes dte=3 → asserts '1-4'.

    E-4 was asserting 'notional_tier' == 'WHALE' — renamed to 'GOLDEN' in
    REARCH-003 deliberation.  Default premium=5100.0 < $50k → 'WATCH'.
    Fix: E-4 passes premium=500_000 (>= _NOTIONAL_GOLDEN=$500k) → asserts 'GOLDEN'.
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure Supabase env vars are unset — persist_flow_episode() must run its
# counter/lock/cache logic regardless of DB connectivity (ING-009-GUARD).
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)

import services.flow_store as fs  # noqa: E402  (import after env teardown)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal_data(
    *,
    ticker: str = "AAPL",
    direction: str = "BULLISH",
    contract_type: str = "CALL",
    strike: float = 150.0,
    expiry: str = "2026-06-20",
    premium: float = 5100.0,
    dte: int = 3,
    size: int = 10,
    trade_type: str = "BTO",
    is_ask_side: bool = True,
) -> dict:
    """Minimal signal_data dict matching the shape accepted by persist_flow_episode().

    Note: dte_bucket and notional_tier are NOT included here.
    persist_flow_episode() computes them from `dte` and `premium` via
    _compute_dte_bucket() / _compute_notional_tier() (SA-3 / REARCH-003).
    Passing stale pre-deliberation values ('0-7d', 'WHALE') would be silently
    ignored and the computed values would differ — see Bug D in the header.
    """
    return {
        "ticker":        ticker,
        "direction":     direction,
        "contract_type": contract_type,
        "strike":        strike,
        "expiry":        expiry,
        "premium":       premium,
        "dte":           dte,
        "size":          size,
        "trade_type":    trade_type,
        "is_ask_side":   is_ask_side,
        "alert":         "WATCH",
        "signal_ts":     "2026-05-12T00:00:00+00:00",
        "occ_symbol":    "AAPL260620C00150000",
    }


def _run_episode(
    signal_data: dict,
    *,
    existing_episode: dict | None = None,
) -> dict:
    """
    Drive persist_flow_episode() for a single INSERT-path call and return the
    captured INSERT payload passed to _insert_rows_with_episode_id().

    Patches:
      - _lookup_open_episode()          → returns existing_episode (None = INSERT path)
      - _insert_rows_with_episode_id()  → captures the row dict, returns True
      - _async_sleep                    → no-op (avoids real delay)

    Bug B fix: _fake_insert matches the real signature
      _insert_rows_with_episode_id(table, row, key, premium, current_oi=None, **kwargs)
    and captures `row` (the second positional arg = the insert payload dict).
    """
    fs._episode_in_flight.clear()
    # Bug A fix: correct dict name is _episode_stats, not _stats.
    fs._episode_stats["created_episodes"] = 0
    fs._episode_stats["merged_episodes"]  = 0
    fs._episode_locks.clear()

    captured: dict = {}

    async def _fake_insert(
        table: str,
        row: dict,
        key: str,
        premium: float,
        current_oi=None,
        **kwargs,
    ) -> bool:
        # Capture the insert payload (row dict) for assertion.
        captured.update(row)
        # Simulate PostgREST returning an id so _set_episode_in_flight is called.
        fs._set_episode_in_flight(
            key,
            row_id=999,
            trade_count=1,
            total_premium=row.get("total_premium", 0.0),
            ask_side_count=row.get("ask_side_count", 0),
        )
        return True

    async def _run():
        with patch(
            "services.flow_store._lookup_open_episode",
            new=AsyncMock(return_value=existing_episode),
        ), patch(
            "services.flow_store._insert_rows_with_episode_id",
            new=AsyncMock(side_effect=_fake_insert),
        ), patch(
            "services.flow_store._async_sleep",
            new=AsyncMock(),
        ):
            await fs.persist_flow_episode(signal_data)

    asyncio.run(_run())
    return captured


def _run_episode_patch(signal_data: dict, in_flight_episode: dict) -> dict:
    """
    Drive persist_flow_episode() for an in-flight PATCH path and return the
    PATCH payload sent to the httpx client.

    Seeds _episode_in_flight with in_flight_episode so the function takes the
    in-flight branch. Patches httpx.AsyncClient.patch() to capture the payload.

    Bug C fix: merge key uses "|" separators to match flow_store._episode_key().
    Old code used ":" separators which caused a key-miss — in_flight was never
    found and the test silently fell through to the INSERT path.
    """
    fs._episode_in_flight.clear()
    fs._episode_locks.clear()
    # Bug A fix: correct dict name.
    fs._episode_stats["created_episodes"] = 0
    fs._episode_stats["merged_episodes"]  = 0

    # Mirror _episode_key() exactly: "|" separators.
    merge_key = (
        f"{signal_data['ticker']}|{signal_data['direction']}|"
        f"{signal_data['contract_type']}|{signal_data['strike']}|"
        f"{signal_data['expiry']}"
    )
    fs._episode_in_flight[merge_key] = in_flight_episode

    captured_patch: dict = {}

    async def _fake_patch(url, **kwargs):
        captured_patch.update(kwargs.get("json", {}))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        return mock_resp

    async def _run():
        with patch(
            "services.flow_store._lookup_open_episode",
            new=AsyncMock(return_value=None),  # forces in-flight branch
        ), patch(
            "services.flow_store._insert_rows_with_episode_id",
            new=AsyncMock(return_value=False),  # should not be called
        ), patch(
            "services.flow_store._async_sleep",
            new=AsyncMock(),
        ), patch(
            "httpx.AsyncClient.patch",
            new=AsyncMock(side_effect=_fake_patch),
        ), patch(
            "services.flow_store._is_configured",
            return_value=True,
        ):
            await fs.persist_flow_episode(signal_data)

    asyncio.run(_run())
    return captured_patch


def _run_episode_db_lookup_patch(signal_data: dict, existing_db_row: dict) -> dict:
    """
    Drive persist_flow_episode() for the DB-lookup PATCH path and return the
    PATCH payload sent to the httpx client.

    This is the distinct second PATCH branch: _episode_in_flight is empty
    (cache miss / cold-start) and _lookup_open_episode returns an existing
    DB row, triggering the DB-lookup PATCH code path.

    Used by E-7b to assert SA-3 compliance on this branch independently of
    the in-flight PATCH branch tested by E-7.

    PBE-1: existing_db_row may have ask_side_count=None (pre-REARCH row);
    the production code uses `(existing.get("ask_side_count") or 0)` to
    COALESCE before incrementing.
    """
    # Leave _episode_in_flight empty — forces DB-lookup branch.
    fs._episode_in_flight.clear()
    fs._episode_locks.clear()
    fs._episode_stats["created_episodes"] = 0
    fs._episode_stats["merged_episodes"]  = 0

    captured_patch: dict = {}

    async def _fake_patch(url, **kwargs):
        captured_patch.update(kwargs.get("json", {}))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        return mock_resp

    async def _run():
        with patch(
            # Returns the existing DB row — triggers the DB-lookup PATCH branch.
            "services.flow_store._lookup_open_episode",
            new=AsyncMock(return_value=existing_db_row),
        ), patch(
            # Should NOT be called on a PATCH path.
            "services.flow_store._insert_rows_with_episode_id",
            new=AsyncMock(return_value=False),
        ), patch(
            "services.flow_store._async_sleep",
            new=AsyncMock(),
        ), patch(
            "httpx.AsyncClient.patch",
            new=AsyncMock(side_effect=_fake_patch),
        ), patch(
            "services.flow_store._is_configured",
            return_value=True,
        ):
            await fs.persist_flow_episode(signal_data)

    asyncio.run(_run())
    return captured_patch


# ---------------------------------------------------------------------------
# QA-1: INSERT path seeds all four aggregate columns (E-1 through E-6)
# ---------------------------------------------------------------------------

class TestInsertPathSeedsAggregateColumns:
    """
    QA-1: Every INSERT must write ask_side_count, ask_side_pct, dte_bucket,
    and notional_tier into the payload from the seed event.
    """

    def test_e1_ask_side_seed_sets_count_and_pct(self):
        """
        E-1: is_ask_side=True on seed event → ask_side_count=1, ask_side_pct=1.0.
        First event in a new episode: 1 ask-side trade out of 1 total = 100%.
        """
        payload = _run_episode(_make_signal_data(is_ask_side=True))
        assert payload.get("ask_side_count") == 1, (
            f"Expected ask_side_count=1 for ask-side seed, got {payload.get('ask_side_count')!r}"
        )
        assert payload.get("ask_side_pct") == pytest.approx(1.0, rel=1e-4), (
            f"Expected ask_side_pct=1.0 for ask-side seed, got {payload.get('ask_side_pct')!r}"
        )

    def test_e2_bid_side_seed_sets_count_and_pct(self):
        """
        E-2: is_ask_side=False on seed event → ask_side_count=0, ask_side_pct=0.0.
        First event is bid-side: 0 ask-side trades out of 1 total = 0%.
        """
        payload = _run_episode(_make_signal_data(is_ask_side=False))
        assert payload.get("ask_side_count") == 0, (
            f"Expected ask_side_count=0 for bid-side seed, got {payload.get('ask_side_count')!r}"
        )
        assert payload.get("ask_side_pct") == pytest.approx(0.0, abs=1e-4), (
            f"Expected ask_side_pct=0.0 for bid-side seed, got {payload.get('ask_side_pct')!r}"
        )

    def test_e3_dte_bucket_written_to_insert_payload(self):
        """
        E-3: dte_bucket computed from signal_data['dte'] must appear in the
        INSERT payload.  SA-3 semantics: value is locked at episode open from
        the seed event's raw dte field via _compute_dte_bucket().

        dte=3 → _compute_dte_bucket(3) → '1-4'  (1 <= 3 <= _DTE_NEAR_MAX=4)

        Bug D: previous test passed dte_bucket='0-7d' directly — not a valid
        _compute_dte_bucket() output.  Production ignores signal_data['dte_bucket']
        and recomputes from signal_data['dte']; without 'dte' in the fixture,
        None → '90+'.  Fix: supply dte=3, assert against computed value '1-4'.
        """
        payload = _run_episode(_make_signal_data(dte=3))
        assert payload.get("dte_bucket") == "1-4", (
            f"Expected dte_bucket='1-4' (dte=3 → _compute_dte_bucket) in INSERT payload, "
            f"got {payload.get('dte_bucket')!r}"
        )

    def test_e4_notional_tier_written_to_insert_payload(self):
        """
        E-4: notional_tier computed from signal_data['premium'] must appear in
        the INSERT payload.  SA-3 semantics: value is locked at episode open
        from the seed event's raw premium via _compute_notional_tier().

        premium=500_000 → _compute_notional_tier(500_000) → 'GOLDEN'
        (_NOTIONAL_GOLDEN threshold = $500k, inclusive)

        Bug D: previous test passed notional_tier='WHALE' — renamed to 'GOLDEN'
        in REARCH-003 deliberation.  Production ignores signal_data['notional_tier']
        and recomputes from signal_data['premium']; default premium=5100.0 < $50k
        → 'WATCH'.  Fix: supply premium=500_000, assert against 'GOLDEN'.
        """
        payload = _run_episode(_make_signal_data(premium=500_000))
        assert payload.get("notional_tier") == "GOLDEN", (
            f"Expected notional_tier='GOLDEN' (premium=$500k → _compute_notional_tier) "
            f"in INSERT payload, got {payload.get('notional_tier')!r}"
        )

    def test_e5_missing_is_ask_side_defaults_to_false(self):
        """
        E-5: signal_data without 'is_ask_side' key → treated as False.
        ask_side_count must be 0, ask_side_pct must be 0.0.
        Guards against KeyError when upstream omits the field.
        """
        sd = _make_signal_data()
        sd.pop("is_ask_side", None)  # deliberately remove the key
        payload = _run_episode(sd)
        assert payload.get("ask_side_count") == 0, (
            f"Missing is_ask_side should default to 0 count, got {payload.get('ask_side_count')!r}"
        )
        assert payload.get("ask_side_pct") == pytest.approx(0.0, abs=1e-4), (
            f"Missing is_ask_side should default to 0.0 pct, got {payload.get('ask_side_pct')!r}"
        )

    def test_e6_created_episodes_counter_increments(self):
        """
        E-6: created_episodes stat counter must increment by 1 after a
        successful INSERT (ING-009 counter contract).
        """
        fs._episode_stats["created_episodes"] = 0
        _run_episode(_make_signal_data())
        assert fs._episode_stats["created_episodes"] == 1, (
            f"Expected created_episodes=1 after INSERT, got {fs._episode_stats['created_episodes']}"
        )


# ---------------------------------------------------------------------------
# QA-2: PATCH payload guard — dte_bucket / notional_tier excluded (E-7, E-7b)
# ---------------------------------------------------------------------------

class TestPatchPayloadExcludesLockedColumns:
    """
    QA-2: SA-3 seed-only semantics — dte_bucket and notional_tier must NEVER
    appear in any PATCH payload. They are locked at episode open and must not
    be overwritten by subsequent events in the same episode.

    Two distinct PATCH code branches exist in persist_flow_episode():
      E-7  — in-flight branch  (episode found in _episode_in_flight cache)
      E-7b — DB-lookup branch  (cache miss; episode found via _lookup_open_episode)
    Both must be tested independently for SA-3 compliance.
    """

    def test_e7_in_flight_patch_excludes_dte_bucket_and_notional_tier(self):
        """
        E-7: When patching an in-flight episode, dte_bucket and notional_tier
        must be absent from the PATCH payload even if signal_data carries them.

        Also validates that ask_side_count and ask_side_pct ARE present
        (the increment must still happen), and that merged_episodes increments.
        """
        # Seed the in-flight cache as if an episode was already opened
        in_flight = {
            "id":              999,
            "trade_count":     1,
            "total_premium":   5100.0,
            "ask_side_count":  1,   # first event was ask-side
        }
        sd = _make_signal_data(is_ask_side=False)
        patch_payload = _run_episode_patch(sd, in_flight_episode=in_flight)

        # SA-3: locked columns must be absent
        assert "dte_bucket" not in patch_payload, (
            f"SA-3 violation: dte_bucket must not appear in PATCH payload. "
            f"Got keys: {list(patch_payload.keys())}"
        )
        assert "notional_tier" not in patch_payload, (
            f"SA-3 violation: notional_tier must not appear in PATCH payload. "
            f"Got keys: {list(patch_payload.keys())}"
        )

        # ask_side_count and ask_side_pct must be updated
        assert "ask_side_count" in patch_payload, (
            "ask_side_count must be present in PATCH payload (running aggregate)"
        )
        assert "ask_side_pct" in patch_payload, (
            "ask_side_pct must be present in PATCH payload (running aggregate)"
        )

        # Second event is bid-side; in_flight had ask_side_count=1, trade_count=1.
        # After merge: trade_count=2, ask_side_count=1 → ask_side_pct=0.5
        assert patch_payload["ask_side_pct"] == pytest.approx(0.5, rel=1e-4), (
            f"Expected ask_side_pct=0.5 after 1 ask / 2 total, "
            f"got {patch_payload.get('ask_side_pct')!r}"
        )

    def test_e7b_db_lookup_patch_excludes_dte_bucket_and_notional_tier(self):
        """
        E-7b: SA-3 compliance for the DB-lookup PATCH branch.

        When the in-flight cache is empty (cold-start / evicted entry) and
        _lookup_open_episode returns an existing DB row, persist_flow_episode()
        takes the DB-lookup PATCH branch. This branch is distinct code from
        the in-flight branch — SA-3 must be asserted independently.

        Also exercises PBE-1 NULL contract: existing row has ask_side_count=None
        (pre-REARCH row). The production code's `(existing.get("ask_side_count")
        or 0)` COALESCE must produce a correct ask_side_pct without KeyError
        or ZeroDivisionError.

        Setup:
          - existing DB row: trade_count=3, ask_side_count=None (pre-REARCH NULL)
          - incoming event:  is_ask_side=True
          - Expected after merge: ask_side_count = COALESCE(None,0)+1 = 1
                                  ask_side_pct   = round(1/4, 4) = 0.25
        """
        existing_db_row = {
            "id":              888,
            "trade_count":     3,
            "total_premium":   15300.0,
            "ask_side_count":  None,   # pre-REARCH row — PBE-1 NULL contract
        }
        sd = _make_signal_data(is_ask_side=True)
        patch_payload = _run_episode_db_lookup_patch(sd, existing_db_row=existing_db_row)

        # SA-3: locked columns must be absent from DB-lookup PATCH payload
        assert "dte_bucket" not in patch_payload, (
            f"SA-3 violation (DB-lookup branch): dte_bucket must not appear in PATCH payload. "
            f"Got keys: {list(patch_payload.keys())}"
        )
        assert "notional_tier" not in patch_payload, (
            f"SA-3 violation (DB-lookup branch): notional_tier must not appear in PATCH payload. "
            f"Got keys: {list(patch_payload.keys())}"
        )

        # ask_side_count and ask_side_pct must be present and correctly computed
        assert "ask_side_count" in patch_payload, (
            "ask_side_count must be present in DB-lookup PATCH payload (running aggregate)"
        )
        assert "ask_side_pct" in patch_payload, (
            "ask_side_pct must be present in DB-lookup PATCH payload (running aggregate)"
        )

        # PBE-1: COALESCE(None, 0) + 1 ask-side event out of 4 total = 0.25
        assert patch_payload["ask_side_count"] == 1, (
            f"Expected ask_side_count=1 (COALESCE(NULL,0)+1), "
            f"got {patch_payload.get('ask_side_count')!r}"
        )
        assert patch_payload["ask_side_pct"] == pytest.approx(0.25, rel=1e-4), (
            f"Expected ask_side_pct=0.25 (1 ask / 4 total after NULL COALESCE), "
            f"got {patch_payload.get('ask_side_pct')!r}"
        )


# ---------------------------------------------------------------------------
# QA-3: ask_side_pct precision contract (E-8)
# ---------------------------------------------------------------------------

class TestAskSidePctPrecision:
    """
    QA-3: ask_side_pct must be stored as NUMERIC(5,4) — four decimal places.
    Tests that round(x, 4) is applied before writing, and that the value
    survives a non-trivial fraction without floating-point blowup.
    """

    def test_e8_ask_side_pct_four_decimal_places(self):
        """
        E-8: 1 ask-side event out of 3 total = 0.3333... → stored as 0.3333
        (round to 4dp). Simulates the state after two prior bid-side events
        by seeding an in-flight episode with ask_side_count=0, trade_count=2,
        then merging one ask-side event.
        """
        in_flight = {
            "id":             999,
            "trade_count":    2,
            "total_premium":  10200.0,
            "ask_side_count": 0,   # first two events were bid-side
        }
        sd = _make_signal_data(is_ask_side=True)  # third event is ask-side
        patch_payload = _run_episode_patch(sd, in_flight_episode=in_flight)

        pct = patch_payload.get("ask_side_pct")
        assert pct is not None, "ask_side_pct missing from PATCH payload"

        # 1 / 3 = 0.33333... → rounded to 4dp = 0.3333
        expected = round(1 / 3, 4)
        assert pct == pytest.approx(expected, rel=1e-4), (
            f"Expected ask_side_pct={expected} (1/3 rounded to 4dp), got {pct!r}"
        )

        # Strict digit-count guard: NUMERIC(5,4) means at most 4 decimal places
        pct_str = f"{pct:.10f}".rstrip("0")
        decimal_digits = len(pct_str.split(".")[1]) if "." in pct_str else 0
        assert decimal_digits <= 4, (
            f"ask_side_pct has {decimal_digits} decimal digits — exceeds NUMERIC(5,4) precision. "
            f"Value: {pct!r}"
        )
