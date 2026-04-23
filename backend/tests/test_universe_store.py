"""
tests/test_universe_store.py

DB read/write tests for services/universe_store.py with mocked Supabase.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from services import universe_store


def _make_sb_mock():
    """Return a Supabase client mock with chainable query builder."""
    sb = MagicMock()
    # Make every chained call return a mock that also chains
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
    sb.table.return_value = query
    return sb, query


# ---------------------------------------------------------------------------
# load_fresh_snapshot
# ---------------------------------------------------------------------------
class TestLoadFreshSnapshot:
    def test_returns_symbols_when_fresh_snapshot_exists(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-001"

        # First call: snapshot table
        # Second call: symbols table
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            MagicMock(data=[{"symbol": "AAPL"}, {"symbol": "TSLA"}]),
        ]

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store.load_fresh_snapshot()

        assert result == ["AAPL", "TSLA"]

    def test_returns_none_when_no_fresh_snapshot(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store.load_fresh_snapshot()

        assert result is None

    def test_returns_none_on_db_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            result = universe_store.load_fresh_snapshot()
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
            result = universe_store.load_any_snapshot()

        assert result == ["SPY", "QQQ"]

    def test_returns_none_when_no_snapshots_at_all(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store.load_any_snapshot()

        assert result is None

    def test_returns_none_on_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            result = universe_store.load_any_snapshot()
        assert result is None


# ---------------------------------------------------------------------------
# save_snapshot
# ---------------------------------------------------------------------------
class TestSaveSnapshot:
    def test_returns_true_on_success(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-new"

        # insert snapshot → deactivate others → prune: select all
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id}]),   # snapshot insert
            MagicMock(data=[]),                        # symbols batch insert
            MagicMock(data=[]),                        # deactivate others
            MagicMock(data=[{"id": snapshot_id}]),    # prune: select all
        ]

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store.save_snapshot(["AAPL", "TSLA"], "tradier_validated")

        assert result is True

    def test_returns_false_on_empty_symbols(self):
        result = universe_store.save_snapshot([], "tradier_validated")
        assert result is False

    def test_returns_false_when_insert_returns_no_row(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])  # empty insert result

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store.save_snapshot(["AAPL"], "tradier_validated")

        assert result is False

    def test_returns_false_on_db_exception(self):
        with patch("services.universe_store._client", side_effect=Exception("DB down")):
            result = universe_store.save_snapshot(["AAPL"], "tradier_validated")
        assert result is False

    def test_prunes_when_over_limit(self):
        sb, query = _make_sb_mock()
        snapshot_id = "snap-new"

        # Create 9 existing snapshots (> _KEEP_SNAPSHOTS=7)
        existing = [{"id": f"snap-{i}"} for i in range(9)]
        existing[0]["id"] = snapshot_id

        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id}]),  # insert
            MagicMock(data=[]),                       # symbols insert
            MagicMock(data=[]),                       # deactivate
            MagicMock(data=existing),                 # prune: select all
            MagicMock(data=[]),                       # prune: delete
        ]

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store.save_snapshot(["AAPL"], "tradier_validated")

        assert result is True
        # Verify delete was called (prune path executed)
        delete_calls = [c for c in query.method_calls if c[0] == "delete"]
        assert len(delete_calls) >= 1

    def test_batches_large_symbol_lists(self):
        """Lists > 500 symbols should be split into multiple batch inserts."""
        sb, query = _make_sb_mock()
        snapshot_id = "snap-big"
        big_symbols = [f"SYM{i}" for i in range(1200)]

        # We need: 1 insert (snapshot) + 3 batches (500+500+200) + 1 deactivate + 1 prune select
        execute_results = (
            [MagicMock(data=[{"id": snapshot_id}])]   # snapshot insert
            + [MagicMock(data=[]) for _ in range(3)]   # 3 symbol batches
            + [MagicMock(data=[]),                      # deactivate
               MagicMock(data=[{"id": snapshot_id}])]  # prune select
        )
        query.execute.side_effect = execute_results

        with patch("services.universe_store._client", return_value=sb):
            result = universe_store.save_snapshot(big_symbols, "tradier_validated")

        assert result is True
