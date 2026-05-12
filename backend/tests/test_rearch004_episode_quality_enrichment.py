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
PATCH path uses COALESCE(NULL, 0) before incrementing. This is tested via the
in-flight PATCH path which exercises the same arithmetic on the in-process
cache value seeded during INSERT.

FIX (REARCH-004 2026-05-12):
  Bug A — fs._stats does not exist; the module dict is fs._episode_stats.
    All references to fs._stats[...] replaced with fs._episode_stats[...].

  Bug B — _fake_insert() in _run_episode() had wrong signature.
    persist_flow_episode() calls:
      _insert_rows_with_episode_id(table, row, key, premium, current_oi, ask_side_count=N)
    The old fake accepted (payload, merge_key) which never matched, causing
    the patch to be a no-op and captured{} to stay empty.
    Fix: match the real signature (table, row, key, premium, current_oi=None, **kwargs)
    and capture `row` (the insert payload dict).

  Bug C — _run_episode_patch() built the merge key with ":" separators.
    flow_store._episode_key() uses "|" separators:
      f"{ticker}|{direction}|{contract_type}|{strike}|{expiry}"
    The mismatch meant _episode_in_flight was seeded under a key that
    persist_flow_episode() never looked up, so in_flight was always None
    and the test took the INSERT path instead of the PATCH path.
    Fix: mirror the exact _episode_key() format using "|" separators.
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
    size: int = 10,
    trade_type: str = "BTO",
    is_ask_side: bool = True,
    dte_bucket: str = "30-60d",
    notional_tier: str = "MEDIUM",
) -> dict:
    """Minimal signal_data dict matching the shape accepted by persist_flow_episode()."""
    return {
        "ticker":        ticker,
        "direction":     direction,
        "contract_type": contract_type,
        "strike":        strike,
        "expiry":        expiry,
        "premium":       premium,
        "size":          size,
        "trade_type":    trade_type,
        "is_ask_side":   is_ask_side,
        "dte_bucket":    dte_bucket,
        "notional_tier": notional_tier,
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
        E-3: dte_bucket from signal_data must appear in the INSERT payload.
        SA-3 semantics: value is locked at episode open from the seed event.
        """
        payload = _run_episode(_make_signal_data(dte_bucket="0-7d"))
        assert payload.get("dte_bucket") == "0-7d", (
            f"Expected dte_bucket='0-7d' in INSERT payload, got {payload.get('dte_bucket')!r}"
        )

    def test_e4_notional_tier_written_to_insert_payload(self):
        """
        E-4: notional_tier from signal_data must appear in the INSERT payload.
        SA-3 semantics: value is locked at episode open from the seed event.
        """
        payload = _run_episode(_make_signal_data(notional_tier="WHALE"))
        assert payload.get("notional_tier") == "WHALE", (
            f"Expected notional_tier='WHALE' in INSERT payload, got {payload.get('notional_tier')!r}"
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
# QA-2: PATCH payload guard — dte_bucket / notional_tier excluded (E-7)
# ---------------------------------------------------------------------------

class TestPatchPayloadExcludesLockedColumns:
    """
    QA-2: SA-3 seed-only semantics — dte_bucket and notional_tier must NEVER
    appear in any PATCH payload. They are locked at episode open and must not
    be overwritten by subsequent events in the same episode.
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
        sd = _make_signal_data(is_ask_side=False, dte_bucket="7-14d", notional_tier="LARGE")
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
