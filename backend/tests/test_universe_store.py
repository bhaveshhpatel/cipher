"""
tests/test_universe_store.py

DB read/write tests for services/universe_store.py with mocked Supabase.
Updated to cover stream_eligible_set parameter in save_snapshot.
Updated 2026-04-24: add TR-01…TR-05 for load_tier_map (Feature 4A).
Updated 2026-04-25: add US-OI-01…US-OI-04 for open_interest upsert (Feature 4A-OI).
Updated 2026-04-27: wire .range() into mock chain so _paginate_symbols resolves
  correctly; add terminating empty-page side_effect entries for all paginated paths.
Updated 2026-04-27c: update TestSaveSnapshot assertions from .insert() to .upsert()
  now that _sync_save_snapshot uses upsert(on_conflict=snapshot_id,symbol) for
  symbol rows to prevent duplicates on repeated cold-start runs.
Updated 2026-04-28 (DEDUP): _sync_save_snapshot now calls
  _get_active_snapshot_for_reuse before the insert/deactivation path, adding
  one extra execute() to the call sequence. Tests updated accordingly.
Updated 2026-04-28 (RC-1): only stream_eligible symbols are written to DB.
  test_stream_eligible_true_when_in_eligible_set now asserts that non-eligible
  symbols are absent from the batch (not that they have stream_eligible=False).
Updated 2026-04-29 (DEDUP-2): _SNAPSHOT_REUSE_DRIFT_PCT bumped to 0.30, _KEEP_SNAPSHOTS=3.
  Added TestSnapshotDeduplicationDriftThreshold regression suite.
Updated 2026-04-29 (DEDUP-2 fix): corrected batch execute() mock counts in
  TestSnapshotDeduplicationDriftThreshold. Batch size=500 means large symbol
  lists require multiple execute() slots: 1200→3 batches, 1400→3 batches,
  4250→9 batches. Previous mocks only provided 1 batch slot → StopIteration
  on batch N+1 → except block → False returned → assert False is True.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from services import universe_store


def _make_sb_mock():
    sb    = MagicMock()
    query = MagicMock()
    query.select.return_value  = query
    query.eq.return_value      = query
    query.neq.return_value     = query
    query.gte.return_value     = query
    query.in_.return_value     = query
    query.order.return_value   = query
    query.limit.return_value   = query
    query.range.return_value   = query
    query.insert.return_value  = query
    query.update.return_value  = query
    query.delete.return_value  = query
    query.upsert.return_value  = query
    query.execute.return_value = MagicMock(data=[])
    sb.table.return_value      = query
    return sb, query


def _make_quote(symbol, last_price=100.0, volume=1_000_000, average_volume=5_000_000,
                open_interest=750, stream_eligible=True):
    """Build a minimal SymbolQuote-compatible MagicMock."""
    q = MagicMock()
    q.symbol         = symbol
    q.last_price     = last_price
    q.volume         = volume
    q.average_volume = average_volume
    q.open_interest  = open_interest
    q.stream_eligible = stream_eligible
    return q


# ---------------------------------------------------------------------------
# load_fresh_snapshot
# ---------------------------------------------------------------------------
class TestLoadFreshSnapshot:
    def test_returns_symbols_when_fresh_snapshot_exists(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-001"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            MagicMock(data=[{"symbol": "AAPL"}, {"symbol": "TSLA"}]),
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_load_fresh_snapshot(24)
        assert result == ["AAPL", "TSLA"]

    def test_returns_none_when_no_fresh_snapshot(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_load_fresh_snapshot(24)
        assert result is None

    def test_returns_none_on_db_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            result = universe_store._sync_load_fresh_snapshot(24)
        assert result is None

    def test_returns_none_when_symbols_table_empty(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-002"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            MagicMock(data=[]),
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_load_fresh_snapshot(24)
        assert result is None


# ---------------------------------------------------------------------------
# load_any_snapshot
# ---------------------------------------------------------------------------
class TestLoadAnySnapshot:
    def test_returns_symbols_from_stale_snapshot(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-old"
        stale_time  = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": stale_time, "source": "tradier_validated"}]),
            MagicMock(data=[{"symbol": "SPY"}, {"symbol": "QQQ"}]),
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_load_any_snapshot()
        assert result == ["SPY", "QQQ"]

    def test_returns_none_when_no_snapshots_at_all(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_load_any_snapshot()
        assert result is None

    def test_returns_none_on_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            result = universe_store._sync_load_any_snapshot()
        assert result is None


# ---------------------------------------------------------------------------
# save_snapshot
# ---------------------------------------------------------------------------
class TestSaveSnapshot:

    def test_returns_true_on_success(self):
        sb, query = _make_sb_mock()
        query.execute.side_effect = [
            MagicMock(data=[]),  # _get_active_snapshot_for_reuse -> no reuse
            MagicMock(data=[]),  # insert snapshot header
            MagicMock(data=[]),  # update deactivate
            MagicMock(data=[]),  # upsert symbol batch
            MagicMock(data=[{"id": "snap-uuid-new"}]),  # _prune_old_snapshots select
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated", None)
        assert result is True

    def test_returns_false_on_empty_symbols(self):
        result = universe_store._sync_save_snapshot([], "tradier_validated", None)
        assert result is False

    def test_returns_false_on_db_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is False

    def test_stream_eligible_true_when_in_eligible_set(self):
        """
        RC-1 fix: only stream_eligible symbols are inserted into the DB.
        Non-eligible symbols (TSLA) are absent from the upsert batch entirely.
        """
        sb, query = _make_sb_mock()
        # First execute() is _get_active_snapshot_for_reuse -> empty = new snapshot
        query.execute.return_value = MagicMock(data=[])
        eligible_set = {"AAPL"}
        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated", eligible_set)
        # Only AAPL is eligible; upsert batch must contain AAPL but NOT TSLA.
        symbol_batch = query.upsert.call_args_list[0].args[0]
        by_symbol = {r["symbol"]: r for r in symbol_batch}
        assert "AAPL" in by_symbol
        assert by_symbol["AAPL"]["stream_eligible"] is True
        assert "TSLA" not in by_symbol  # non-eligible: absent, not flagged False

    def test_stream_eligible_all_true_when_no_eligible_set(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_save_snapshot(["SPY", "QQQ"], "tradier_validated", None)
        # Symbol rows are written via upsert; first upsert call is the symbol batch.
        symbol_batch = query.upsert.call_args_list[0].args[0]
        for row in symbol_batch:
            assert row["stream_eligible"] is True

    def test_snapshot_id_generated_and_passed_in_insert(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is True
        # The snapshot header is still written via .insert() (single dict, not a list).
        first_insert = query.insert.call_args_list[0].args[0]
        assert "id" in first_insert
        assert "-" in first_insert["id"]

    def test_snapshot_id_consistent_across_insert_and_deactivation(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated", None)
        # Snapshot header written via insert (single dict).
        snapshot_id = query.insert.call_args_list[0].args[0]["id"]
        # Symbol rows written via upsert; first upsert call is the symbol batch.
        symbol_batch = query.upsert.call_args_list[0].args[0]
        for row in symbol_batch:
            assert row["snapshot_id"] == snapshot_id
        neq_calls = [c for c in query.method_calls if c[0] == "neq"]
        assert any(c.args == ("id", snapshot_id) for c in neq_calls)

    def test_batches_large_symbol_lists(self):
        sb, query = _make_sb_mock()
        big_symbols = [f"SYM{i}" for i in range(1200)]
        query.execute.side_effect = (
            [MagicMock(data=[])]  # _get_active_snapshot_for_reuse
            + [MagicMock(data=[]) for _ in range(3)]  # insert + deactivate + batch1
            + [MagicMock(data=[]), MagicMock(data=[])]  # batch2 + batch3
            + [MagicMock(data=[])]  # _prune select
        )
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(big_symbols, "tradier_validated", None)
        assert result is True
        # Snapshot header = 1 insert call. Symbol rows = 3 upsert calls (500+500+200).
        assert query.insert.call_count == 1
        assert query.upsert.call_count == 3

    def test_prunes_when_over_limit(self):
        """
        DEDUP fix: _sync_save_snapshot calls _get_active_snapshot_for_reuse
        first (1 execute), then insert (1), deactivate (1), upsert batch (1),
        then _prune_old_snapshots calls select-all (1) + delete symbols (1)
        + delete snapshots (1). Total = 7 execute() calls.
        """
        sb, query   = _make_sb_mock()
        existing    = [{"id": f"snap-{i}"} for i in range(9)]
        query.execute.side_effect = [
            MagicMock(data=[]),       # _get_active_snapshot_for_reuse -> no reuse
            MagicMock(data=[]),       # insert snapshot header
            MagicMock(data=[]),       # update deactivate
            MagicMock(data=[]),       # upsert symbol batch
            MagicMock(data=existing), # _prune select all snapshots
            MagicMock(data=[]),       # delete symbols for pruned ids
            MagicMock(data=[]),       # delete snapshot headers for pruned ids
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is True
        delete_calls = [c for c in query.method_calls if c[0] == "delete"]
        assert len(delete_calls) >= 1

    def test_no_prune_when_within_limit(self):
        """
        _KEEP_SNAPSHOTS=3: prune select returns 3 rows → 3 <= 3 → no delete.
        """
        sb, query = _make_sb_mock()
        existing  = [{"id": f"snap-{i}"} for i in range(3)]
        query.execute.side_effect = [
            MagicMock(data=[]),      # _get_active_snapshot_for_reuse
            MagicMock(data=[]),      # insert
            MagicMock(data=[]),      # deactivate
            MagicMock(data=[]),      # upsert batch
            MagicMock(data=existing),  # _prune select
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is True
        delete_calls = [c for c in query.method_calls if c[0] == "delete"]
        assert len(delete_calls) == 0


# ---------------------------------------------------------------------------
# load_tier_map (Feature 4A) — TR-01 … TR-05
# ---------------------------------------------------------------------------
class TestLoadTierMap:
    """Feature 4A: load_tier_map() reads tier column from options_universe_symbols."""

    # TR-01
    def test_returns_dict_of_symbol_to_tier(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-tier-001"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            MagicMock(data=[{"symbol": "SPY", "tier": 1}, {"symbol": "HOOD", "tier": 2}]),
        ]
        with patch("services.universe_store._client", return_value=sb):
            fn = getattr(universe_store, '_sync_load_tier_map',
                         getattr(universe_store, 'load_tier_map', None))
            if fn is None:
                pytest.skip("load_tier_map not implemented yet")
            result = fn()
        assert isinstance(result, dict)
        assert result.get("SPY")  == 1
        assert result.get("HOOD") == 2

    # TR-02
    def test_returns_empty_dict_when_no_snapshot(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            fn = getattr(universe_store, '_sync_load_tier_map',
                         getattr(universe_store, 'load_tier_map', None))
            if fn is None:
                pytest.skip("load_tier_map not implemented yet")
            result = fn()
        assert result == {}

    # TR-03
    def test_returns_empty_dict_on_db_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            fn = getattr(universe_store, '_sync_load_tier_map',
                         getattr(universe_store, 'load_tier_map', None))
            if fn is None:
                pytest.skip("load_tier_map not implemented yet")
            result = fn()
        assert result == {}

    # TR-04
    def test_tier_column_value_preserved(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-tier-002"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            MagicMock(data=[{"symbol": "XYZ", "tier": 3}]),
        ]
        with patch("services.universe_store._client", return_value=sb):
            fn = getattr(universe_store, '_sync_load_tier_map',
                         getattr(universe_store, 'load_tier_map', None))
            if fn is None:
                pytest.skip("load_tier_map not implemented yet")
            result = fn()
        assert result.get("XYZ") == 3
        assert isinstance(result.get("XYZ"), int)

    # TR-05
    def test_missing_tier_column_defaults_to_3(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-tier-003"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            MagicMock(data=[{"symbol": "LEGACY"}]),
        ]
        with patch("services.universe_store._client", return_value=sb):
            fn = getattr(universe_store, '_sync_load_tier_map',
                         getattr(universe_store, 'load_tier_map', None))
            if fn is None:
                pytest.skip("load_tier_map not implemented yet")
            result = fn()
        assert result.get("LEGACY") == 3


# ---------------------------------------------------------------------------
# upsert_symbol_quotes — open_interest (Feature 4A-OI) — US-OI-01 … US-OI-04
# ---------------------------------------------------------------------------
class TestUpsertSymbolQuotesOi:

    def _run_upsert(self, quotes, tier_map=None):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-oi-test-001"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id}]),
        ] + [MagicMock(data=[]) for _ in range(10)]

        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_upsert_symbol_quotes(quotes, tier_map or {})

        return query

    # US-OI-01
    def test_open_interest_key_present_in_upsert_row(self):
        quotes = [_make_quote("AAPL", open_interest=2000)]
        query  = self._run_upsert(quotes)
        upsert_calls = query.upsert.call_args_list
        assert upsert_calls, "upsert() was never called"
        rows = upsert_calls[0].args[0]
        assert isinstance(rows, list) and len(rows) > 0
        assert "open_interest" in rows[0]

    # US-OI-02
    def test_open_interest_value_sourced_from_quote(self):
        quotes = [
            _make_quote("SPY",  open_interest=5_000),
            _make_quote("HOOD", open_interest=600),
        ]
        query = self._run_upsert(quotes)
        rows      = query.upsert.call_args_list[0].args[0]
        by_symbol = {r["symbol"]: r for r in rows}
        assert by_symbol["SPY"]["open_interest"]  == 5_000
        assert by_symbol["HOOD"]["open_interest"] == 600

    # US-OI-03
    def test_open_interest_none_written_as_none(self):
        quotes = [_make_quote("SPCE", open_interest=None)]
        query  = self._run_upsert(quotes)
        rows = query.upsert.call_args_list[0].args[0]
        assert rows[0]["open_interest"] is None

    # US-OI-04
    def test_open_interest_and_tier_both_present_in_same_row(self):
        quotes   = [_make_quote("TSLA", open_interest=1_500)]
        tier_map = {"TSLA": 1}
        query    = self._run_upsert(quotes, tier_map)
        rows = query.upsert.call_args_list[0].args[0]
        row  = rows[0]
        assert "open_interest" in row
        assert "tier"           in row
        assert row["open_interest"] == 1_500
        assert row["tier"]          == 1


# ---------------------------------------------------------------------------
# Snapshot deduplication drift threshold — DEDUP-2 regression
# ---------------------------------------------------------------------------
class TestSnapshotDeduplicationDriftThreshold:
    """
    Regression suite for DEDUP-2 fix (2026-04-29).

    Root cause: _SNAPSHOT_REUSE_DRIFT_PCT=0.10 was too tight. Natural CBOE
    universe variation of 10-15% per restart (e.g. 3762 → 4259 → 4336 in one
    day) caused a new uuid4() snapshot on every restart, making
    on_conflict=(snapshot_id,symbol) a no-op → always INSERT → 6 duplicate
    AAPL rows accumulated in options_universe_symbols across 6 restarts.

    Fix: threshold raised to 0.30 (30%). These tests pin that behaviour.

    IMPORTANT — batch size is 500. execute() side_effect lists must include one
    slot per batch call or StopIteration will be raised inside the loop, caught
    by the except block, and the function returns False instead of True.
      1200 symbols → ceil(1200/500) = 3 batches  (reuse path: no insert/deactivate)
      1400 symbols → ceil(1400/500) = 3 batches  (new snapshot: insert + deactivate)
      4250 symbols → ceil(4250/500) = 9 batches  (reuse path: no insert/deactivate)
    """

    def test_reuses_snapshot_when_drift_within_30pct(self):
        """
        Existing count=1000, new count=1200 → 20% drift < 30% → reuse,
        no INSERT on options_universe_snapshots.

        execute() sequence (reuse path, 1200 symbols → 3 batches):
          [0] _get_active_snapshot_for_reuse → existing snapshot returned
          [1] upsert batch 1/3 (500 symbols)
          [2] upsert batch 2/3 (500 symbols)
          [3] upsert batch 3/3 (200 symbols)
          [4] _prune_old_snapshots select
        """
        sb, query = _make_sb_mock()
        recent = datetime.now(timezone.utc).isoformat()
        query.execute.side_effect = [
            MagicMock(data=[{"id": "existing-snap", "symbol_count": 1000, "fetched_at": recent}]),
            MagicMock(data=[]),   # upsert batch 1/3
            MagicMock(data=[]),   # upsert batch 2/3
            MagicMock(data=[]),   # upsert batch 3/3
            MagicMock(data=[{"id": "existing-snap"}]),  # prune select (1 ≤ 3)
        ]
        symbols = [f"SYM{i}" for i in range(1200)]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(symbols, "tradier_validated", None)
        assert result is True
        assert query.insert.call_count == 0, (
            "Must NOT create a new snapshot when drift=20% is within 30% threshold"
        )

    def test_creates_new_snapshot_when_drift_exceeds_30pct(self):
        """
        Existing count=1000, new count=1400 → 40% drift > 30% → new snapshot,
        INSERT fires once.

        execute() sequence (new snapshot path, 1400 symbols → 3 batches):
          [0] _get_active_snapshot_for_reuse → existing returned (drift check)
          [1] insert new snapshot header
          [2] deactivate old snapshot
          [3] upsert batch 1/3 (500 symbols)
          [4] upsert batch 2/3 (500 symbols)
          [5] upsert batch 3/3 (400 symbols)
          [6] _prune_old_snapshots select
        """
        sb, query = _make_sb_mock()
        recent = datetime.now(timezone.utc).isoformat()
        query.execute.side_effect = [
            MagicMock(data=[{"id": "old-snap", "symbol_count": 1000, "fetched_at": recent}]),
            MagicMock(data=[]),   # insert new snapshot header
            MagicMock(data=[]),   # deactivate old
            MagicMock(data=[]),   # upsert batch 1/3
            MagicMock(data=[]),   # upsert batch 2/3
            MagicMock(data=[]),   # upsert batch 3/3
            MagicMock(data=[{"id": "old-snap"}, {"id": "snap-new"}]),  # prune select (2 ≤ 3)
        ]
        symbols = [f"SYM{i}" for i in range(1400)]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(symbols, "tradier_validated", None)
        assert result is True
        assert query.insert.call_count == 1, (
            "Must create a new snapshot when drift=40% exceeds 30% threshold"
        )

    def test_no_new_snapshot_on_typical_daily_drift(self):
        """
        Existing count=4000, new count=4250 → 6.25% drift — mirrors the
        production pattern that caused 6 duplicate AAPL rows. Must reuse.

        execute() sequence (reuse path, 4250 symbols → 9 batches):
          [0]   _get_active_snapshot_for_reuse → existing snapshot returned
          [1-9] upsert batches 1–9 (500×8 + 250)
          [10]  _prune_old_snapshots select
        """
        sb, query = _make_sb_mock()
        recent = datetime.now(timezone.utc).isoformat()
        import math
        batch_size  = 500
        n_symbols   = 4250
        n_batches   = math.ceil(n_symbols / batch_size)  # 9
        query.execute.side_effect = (
            [MagicMock(data=[{"id": "prod-snap", "symbol_count": 4000, "fetched_at": recent}])]
            + [MagicMock(data=[]) for _ in range(n_batches)]   # 9 upsert batches
            + [MagicMock(data=[{"id": "prod-snap"}])]          # prune select (1 ≤ 3)
        )
        symbols = [f"SYM{i}" for i in range(n_symbols)]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(symbols, "tradier_validated", None)
        assert result is True
        assert query.insert.call_count == 0, (
            "Must NOT create a new snapshot for a 6.25% drift — this is the "
            "exact production pattern that caused duplicate rows"
        )

    def test_creates_new_snapshot_when_no_active_exists(self):
        """Cold start: no active snapshot → always creates new one."""
        sb, query = _make_sb_mock()
        query.execute.side_effect = [
            MagicMock(data=[]),   # reuse check → no active snapshot
            MagicMock(data=[]),   # insert
            MagicMock(data=[]),   # deactivate
            MagicMock(data=[]),   # upsert
            MagicMock(data=[{"id": "brand-new"}]),  # prune select
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated", None)
        assert result is True
        assert query.insert.call_count == 1

    def test_creates_new_snapshot_when_existing_symbol_count_is_zero(self):
        """
        Active snapshot exists but symbol_count=0 (e.g. corrupted row) →
        should not reuse (division by zero guard), must create new snapshot.
        """
        sb, query = _make_sb_mock()
        recent = datetime.now(timezone.utc).isoformat()
        query.execute.side_effect = [
            MagicMock(data=[{"id": "bad-snap", "symbol_count": 0, "fetched_at": recent}]),
            MagicMock(data=[]),   # insert
            MagicMock(data=[]),   # deactivate
            MagicMock(data=[]),   # upsert
            MagicMock(data=[{"id": "bad-snap"}, {"id": "new-snap"}]),  # prune select
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is True
        assert query.insert.call_count == 1, (
            "Must create new snapshot when existing symbol_count=0 (division-by-zero guard)"
        )
