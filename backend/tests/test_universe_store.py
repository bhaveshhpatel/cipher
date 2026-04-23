"""
tests/test_universe_store.py

DB read/write tests for services/universe_store.py with mocked Supabase.
Updated to cover stream_eligible_set parameter in save_snapshot.
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
    query.insert.return_value  = query
    query.update.return_value  = query
    query.delete.return_value  = query
    query.execute.return_value = MagicMock(data=[])
    sb.table.return_value      = query
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
# save_snapshot — including stream_eligible_set
# ---------------------------------------------------------------------------
class TestSaveSnapshot:

    def test_returns_true_on_success(self):
        sb, query = _make_sb_mock()
        query.execute.side_effect = [
            MagicMock(data=[]),  # snapshot insert
            MagicMock(data=[]),  # symbols batch insert
            MagicMock(data=[]),  # deactivate
            MagicMock(data=[{"id": "snap-uuid-new"}]),  # prune select
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
        """Symbols in eligible_set must have stream_eligible=True in insert payload."""
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        eligible_set = {"AAPL"}
        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated", eligible_set)

        symbol_batch = query.insert.call_args_list[1].args[0]
        by_symbol = {r["symbol"]: r for r in symbol_batch}
        assert by_symbol["AAPL"]["stream_eligible"] is True
        assert by_symbol["TSLA"]["stream_eligible"] is False

    def test_stream_eligible_all_true_when_no_eligible_set(self):
        """When eligible_set is None, all symbols default to stream_eligible=True."""
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_save_snapshot(["SPY", "QQQ"], "tradier_validated", None)

        symbol_batch = query.insert.call_args_list[1].args[0]
        for row in symbol_batch:
            assert row["stream_eligible"] is True

    def test_snapshot_id_generated_and_passed_in_insert(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is True
        first_insert = query.insert.call_args_list[0].args[0]
        assert "id" in first_insert
        assert "-" in first_insert["id"]

    def test_snapshot_id_consistent_across_insert_and_deactivation(self):
        sb, query = _make_sb_mock()
        query.execute.return_value = MagicMock(data=[])
        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_save_snapshot(["AAPL", "TSLA"], "tradier_validated", None)

        snapshot_id  = query.insert.call_args_list[0].args[0]["id"]
        symbol_batch = query.insert.call_args_list[1].args[0]
        for row in symbol_batch:
            assert row["snapshot_id"] == snapshot_id

        neq_calls = [c for c in query.method_calls if c[0] == "neq"]
        assert any(c.args == ("id", snapshot_id) for c in neq_calls)

    def test_batches_large_symbol_lists(self):
        sb, query = _make_sb_mock()
        big_symbols = [f"SYM{i}" for i in range(1200)]
        query.execute.side_effect = (
            [MagicMock(data=[])]
            + [MagicMock(data=[]) for _ in range(3)]
            + [MagicMock(data=[]), MagicMock(data=[])]
        )
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(big_symbols, "tradier_validated", None)
        assert result is True
        assert query.insert.call_count == 4

    def test_prunes_when_over_limit(self):
        sb, query   = _make_sb_mock()
        existing    = [{"id": f"snap-{i}"} for i in range(9)]
        query.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=existing),
            MagicMock(data=[]),
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is True
        delete_calls = [c for c in query.method_calls if c[0] == "delete"]
        assert len(delete_calls) >= 1

    def test_no_prune_when_within_limit(self):
        sb, query = _make_sb_mock()
        existing  = [{"id": f"snap-{i}"} for i in range(3)]
        query.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=existing),
        ]
        with patch("services.universe_store._client", return_value=sb):
            result = universe_store._sync_save_snapshot(["AAPL"], "tradier_validated", None)
        assert result is True
        delete_calls = [c for c in query.method_calls if c[0] == "delete"]
        assert len(delete_calls) == 0
