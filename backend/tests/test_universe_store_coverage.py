"""
P2 coverage tests for services/universe_store.py.

Targets uncovered lines:
  - _prune_old_snapshots: len(rows) <= keep → early return, no delete called
  - _prune_old_snapshots: excess rows → delete called with correct ids
  - _prune_old_snapshots: exception → logs warning, does not raise
  - _sync_save_snapshot: empty symbols list → returns False
  - _sync_save_snapshot: exception → returns False
  - _sync_load_fresh_snapshot: exception → returns None
  - _sync_load_fresh_snapshot: no rows → returns None
  - _sync_load_any_snapshot: exception → returns None
  - _sync_load_any_snapshot: no rows → returns None
  - _sync_load_tier_map: no active snapshot → returns {}
  - _sync_load_tier_map: null tier → defaults to 3
  - _sync_load_tier_map: exception → returns {}
  - _sync_upsert_symbol_quotes: no active snapshot → logs warning, returns
  - _sync_upsert_symbol_quotes: exception → logs warning, does not raise
Updated 2026-04-27: wire .range() into mock chain for _paginate_symbols compat.
Updated 2026-04-27d: test_prune_deletes_excess_snapshots now asserts .in_ is
  called twice (symbol rows first, then snapshot headers) after _prune_old_snapshots
  was updated to delete child symbol rows before parent snapshot rows.
"""
from unittest.mock import MagicMock, patch
from services.universe_store import (
    _prune_old_snapshots,
    _sync_save_snapshot,
    _sync_load_fresh_snapshot,
    _sync_load_any_snapshot,
    _sync_load_tier_map,
    _sync_upsert_symbol_quotes,
)


def _mock_sb():
    sb = MagicMock()
    q  = MagicMock()
    for m in ["select", "insert", "update", "upsert", "delete",
              "eq", "neq", "gte", "order", "limit", "in_", "range"]:
        getattr(q, m).return_value = q
    q.execute.return_value = MagicMock(data=[])
    sb.table.return_value = q
    return sb, q


# ---------------------------------------------------------------------------
# _prune_old_snapshots
# ---------------------------------------------------------------------------

def test_prune_no_action_when_under_limit():
    sb, q = _mock_sb()
    q.execute.return_value = MagicMock(data=[{"id": f"s{i}"} for i in range(3)])
    _prune_old_snapshots(sb, keep=7)
    q.in_.assert_not_called()


def test_prune_deletes_excess_snapshots():
    sb, q = _mock_sb()
    rows = [{"id": f"s{i}"} for i in range(10)]
    q.execute.return_value = MagicMock(data=rows)
    excess_ids = [r["id"] for r in rows[7:]]
    _prune_old_snapshots(sb, keep=7)
    # _prune_old_snapshots now deletes child symbol rows first, then snapshot headers.
    # in_ is called twice: once for options_universe_symbols, once for options_universe_snapshots.
    assert q.in_.call_count == 2
    q.in_.assert_any_call("snapshot_id", excess_ids)
    q.in_.assert_any_call("id", excess_ids)


def test_prune_exception_does_not_raise():
    sb = MagicMock()
    sb.table.side_effect = Exception("DB gone")
    _prune_old_snapshots(sb, keep=7)  # must not propagate


# ---------------------------------------------------------------------------
# _sync_save_snapshot
# ---------------------------------------------------------------------------

def test_save_snapshot_empty_symbols_returns_false():
    assert _sync_save_snapshot([], "test") is False


def test_save_snapshot_exception_returns_false():
    with patch("services.universe_store._client", side_effect=RuntimeError("no key")):
        assert _sync_save_snapshot(["AAPL"], "test") is False


# ---------------------------------------------------------------------------
# _sync_load_fresh_snapshot
# ---------------------------------------------------------------------------

def test_load_fresh_snapshot_exception_returns_none():
    with patch("services.universe_store._client", side_effect=RuntimeError("no key")):
        assert _sync_load_fresh_snapshot(24) is None


def test_load_fresh_snapshot_no_rows_returns_none():
    sb, q = _mock_sb()
    q.execute.return_value = MagicMock(data=[])
    with patch("services.universe_store._client", return_value=sb):
        assert _sync_load_fresh_snapshot(24) is None


# ---------------------------------------------------------------------------
# _sync_load_any_snapshot
# ---------------------------------------------------------------------------

def test_load_any_snapshot_exception_returns_none():
    with patch("services.universe_store._client", side_effect=RuntimeError("no key")):
        assert _sync_load_any_snapshot() is None


def test_load_any_snapshot_no_rows_returns_none():
    sb, q = _mock_sb()
    q.execute.return_value = MagicMock(data=[])
    with patch("services.universe_store._client", return_value=sb):
        assert _sync_load_any_snapshot() is None


# ---------------------------------------------------------------------------
# _sync_load_tier_map
# ---------------------------------------------------------------------------

def test_load_tier_map_no_active_snapshot_returns_empty():
    sb, q = _mock_sb()
    q.execute.return_value = MagicMock(data=[])
    with patch("services.universe_store._client", return_value=sb):
        assert _sync_load_tier_map() == {}


def test_load_tier_map_null_tier_defaults_to_3():
    sb, q = _mock_sb()
    q.execute.side_effect = [
        MagicMock(data=[{"id": "snap-1"}]),
        MagicMock(data=[{"symbol": "AAPL", "tier": None}]),
    ]
    with patch("services.universe_store._client", return_value=sb):
        result = _sync_load_tier_map()
    assert result["AAPL"] == 3


def test_load_tier_map_exception_returns_empty():
    with patch("services.universe_store._client", side_effect=RuntimeError("no key")):
        assert _sync_load_tier_map() == {}


# ---------------------------------------------------------------------------
# _sync_upsert_symbol_quotes
# ---------------------------------------------------------------------------

def test_upsert_no_active_snapshot_returns_silently():
    sb, q = _mock_sb()
    q.execute.return_value = MagicMock(data=[])
    quote = MagicMock()
    quote.symbol = "AAPL"
    with patch("services.universe_store._client", return_value=sb):
        _sync_upsert_symbol_quotes([quote], {})  # must not raise


def test_upsert_exception_does_not_raise():
    with patch("services.universe_store._client", side_effect=RuntimeError("no key")):
        quote = MagicMock()
        quote.symbol = "AAPL"
        _sync_upsert_symbol_quotes([quote], {})  # must not raise
