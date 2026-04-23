"""
tests/test_universe_store.py

DB read/write tests for services/universe_store.py with mocked Supabase.

Covers:
  - load_fresh_snapshot: fresh hit, miss, DB exception
  - load_any_snapshot: stale hit, empty, DB exception
  - save_snapshot:
      * success path
      * empty symbol list guard
      * DB exception
      * large list batching (>500 symbols)
      * pruning when over limit
      * uuid4 id generated and passed in insert payload  ← regression for
        the AttributeError: 'SyncQueryRequestBuilder' has no attribute 'select'
        crash that occurred when chaining .insert().select() on supabase-py v2
"""
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta

from services import universe_store


def _make_sb_mock():
    """Return a Supabase client mock with fully chainable query builder."""
    sb = MagicMock()
    query = MagicMock()
    query.select.return_value = query
    query.eq.return_value = query
    query.neq.return_value = query
    query.gte.return_value = query
    query.in_.return_value = query
    query.order.return_value = query
    query.limit.return_value = query
    query.insert.return_value = query
    query.update.return_value = query
    query.delete.return_value = query
    query.execute.return_value = MagicMock(data=[])
    sb.table.return_value = query
    return sb, query


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
            MagicMock(data=[]),  # empty symbols
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
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

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
        snapshot_id = "snap-uuid-new"

        # sequence: insert snapshot, insert symbols batch, deactivate, prune select
        query.execute.side_effect = [
            MagicMock(data=[]),                       # snapshot insert (id pre-generated)
            MagicMock(data=[]),                       # symbols batch insert
            MagicMock(data=[]),                       # deactivate others
            MagicMock(data=[{"id": snapshot_id}]),   # prune: select all
        ]

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated")

        assert result is True

    def test_returns_false_on_empty_symbols(self):
        """Guard clause: empty list must return False without touching DB."""
        result = universe_store._sync_save_snapshot([], "tradier_validated")
        assert result is False

    def test_returns_false_on_db_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated")
        assert result is False

    # ------------------------------------------------------------------
    # Regression: uuid4 snapshot_id fix
    # ------------------------------------------------------------------

    def test_snapshot_id_is_generated_and_passed_in_insert(self):
        """
        Regression test for AttributeError: 'SyncQueryRequestBuilder' has no
        attribute 'select'.

        Previously _sync_save_snapshot chained .insert().select().execute() to
        read back the inserted row's id. supabase-py v2 SyncQueryRequestBuilder
        does not support .select() after .insert(), causing a crash on every
        save_snapshot() call.

        Fix: generate snapshot_id = str(uuid4()) before the insert and pass it
        explicitly in the payload. This test asserts that:
          1. The insert payload contains an 'id' key.
          2. The id looks like a valid UUID (len > 0, contains hyphens).
          3. save_snapshot returns True even though execute() returns empty data.
        """
        sb, query = _make_sb_mock()
        # All execute() calls return empty data — verifies we never rely on
        # reading the id back from the insert response.
        query.execute.return_value = MagicMock(data=[])

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated")

        assert result is True

        # Verify insert was called with an explicit 'id' in the payload
        first_insert_payload = query.insert.call_args_list[0].args[0]
        assert "id" in first_insert_payload, "insert payload must include explicit 'id'"
        generated_id = first_insert_payload["id"]
        assert len(generated_id) > 0
        assert "-" in generated_id, "id should be a UUID (contain hyphens)"

    def test_save_succeeds_even_when_insert_returns_empty_data(self):
        """
        Confirms save_snapshot does not fail when .execute() returns data=[].
        This was the broken behaviour before the uuid4 fix: empty insert data
        meant snapshot_id could never be obtained, so save always returned False.
        """
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["SPY", "QQQ", "NVDA"], "tradier_validated")

        assert result is True

    def test_snapshot_id_used_consistently_for_symbols_and_deactivation(self):
        """
        The same uuid4 id must appear in both the snapshot insert payload AND
        the symbols batch insert payload, and must be used in the neq filter
        for deactivating other snapshots.
        """
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])

        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated")

        # Extract id from snapshot insert
        snapshot_insert_payload = query.insert.call_args_list[0].args[0]
        snapshot_id = snapshot_insert_payload["id"]

        # Symbols batch insert should reference the same snapshot_id
        symbol_insert_payload = query.insert.call_args_list[1].args[0]
        assert isinstance(symbol_insert_payload, list)
        for row in symbol_insert_payload:
            assert row["snapshot_id"] == snapshot_id, (
                f"Symbol row snapshot_id {row['snapshot_id']!r} "
                f"does not match snapshot insert id {snapshot_id!r}"
            )

        # deactivate call: .neq("id", snapshot_id)
        neq_calls = [c for c in query.method_calls if c[0] == "neq"]
        assert any(
            c.args == ("id", snapshot_id) for c in neq_calls
        ), "neq deactivation filter must use the same snapshot_id"

    # ------------------------------------------------------------------
    # Batching & pruning
    # ------------------------------------------------------------------

    def test_batches_large_symbol_lists(self):
        """Lists > 500 symbols must be split into multiple batch inserts."""
        sb, query = _make_sb_mock()
        big_symbols = [f"SYM{i}" for i in range(1200)]

        # 1 snapshot insert + 3 symbol batches (500+500+200) + 1 deactivate + 1 prune select
        query.execute.side_effect = (
            [MagicMock(data=[])]                      # snapshot insert
            + [MagicMock(data=[]) for _ in range(3)]  # 3 symbol batches
            + [MagicMock(data=[]),                     # deactivate
               MagicMock(data=[])]                     # prune select (≤7, no delete)
        )

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(big_symbols, "tradier_validated")

        assert result is True
        # insert called 4 times: 1 snapshot + 3 symbol batches
        assert query.insert.call_count == 4

    def test_prunes_when_over_limit(self):
        """When DB has more than _KEEP_SNAPSHOTS rows, delete must be called."""
        sb, query = _make_sb_mock()
        existing = [{"id": f"snap-{i}"} for i in range(9)]  # 9 > _KEEP_SNAPSHOTS=7

        query.execute.side_effect = [
            MagicMock(data=[]),            # snapshot insert
            MagicMock(data=[]),            # symbols insert
            MagicMock(data=[]),            # deactivate
            MagicMock(data=existing),      # prune: select all (9 rows)
            MagicMock(data=[]),            # prune: delete 2
        ]

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated")

        assert result is True
        delete_calls = [c for c in query.method_calls if c[0] == "delete"]
        assert len(delete_calls) >= 1

    def test_no_prune_when_within_limit(self):
        """When DB has ≤ _KEEP_SNAPSHOTS rows, delete must NOT be called."""
        sb, query = _make_sb_mock()
        existing = [{"id": f"snap-{i}"} for i in range(3)]  # 3 < 7

        query.execute.side_effect = [
            MagicMock(data=[]),           # snapshot insert
            MagicMock(data=[]),           # symbols insert
            MagicMock(data=[]),           # deactivate
            MagicMock(data=existing),     # prune: select all (3 rows — no delete)
        ]

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated")

        assert result is True
        delete_calls = [c for c in query.method_calls if c[0] == "delete"]
        assert len(delete_calls) == 0
