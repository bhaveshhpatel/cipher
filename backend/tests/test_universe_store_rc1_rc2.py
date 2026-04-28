"""
test_universe_store_rc1_rc2.py

100% coverage of the RC-1 and RC-2 fixes in universe_store._sync_save_snapshot:

  RC-1 (S-04): save_snapshot() now inserts ONLY stream_eligible symbols.
               symbol_count in snapshot header = len(eligible_symbols).
  RC-2 (S-05): Non-eligible symbols no longer produce NULL-price rows.
               All rows written to DB are stream_eligible=True by construction.

Tests:
  1.  eligible_only_inserted            — only eligible symbols go into upsert rows
  2.  symbol_count_matches_eligible     — snapshot header uses eligible count, not total
  3.  no_null_rows_for_non_eligible     — zero rows with stream_eligible=False inserted
  4.  all_symbols_eligible_passthrough  — when eligible_set == symbols, all rows written
  5.  empty_eligible_set_fallback       — empty eligible_set falls back to all symbols
  6.  empty_symbols_guard               — empty symbols returns False immediately
  7.  upsert_idempotent_on_conflict     — upsert called with on_conflict param
  8.  snapshot_deactivation_called      — previous snapshots deactivated after insert
  9.  prune_called_after_save           — _prune_old_snapshots called after successful save
  10. none_eligible_set_treated_as_all  — stream_eligible_set=None means all are eligible
  11. rows_all_flagged_stream_eligible  — every row passed to upsert has stream_eligible=True
  12. symbol_count_not_total_cboe_size  — symbol_count != len(all_symbols) when subset eligible
  13. batch_split_correct               — 600 symbols split into 2 batches of 500/100
  14. exception_returns_false           — DB exception returns False gracefully
"""
import pytest
from unittest.mock import MagicMock, patch, call


def _make_sb_mock():
    """Return a mock Supabase client where every chained call succeeds."""
    sb = MagicMock()
    table_mock = MagicMock()
    sb.table.return_value = table_mock
    table_mock.insert.return_value = table_mock
    table_mock.upsert.return_value = table_mock
    table_mock.update.return_value = table_mock
    table_mock.delete.return_value = table_mock
    table_mock.select.return_value = table_mock
    table_mock.eq.return_value = table_mock
    table_mock.neq.return_value = table_mock
    table_mock.order.return_value = table_mock
    table_mock.limit.return_value = table_mock
    table_mock.in_.return_value = table_mock
    table_mock.execute.return_value = MagicMock(data=[])
    return sb


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_eligible_only_inserted(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    all_syms = ["AAPL", "MSFT", "JUNK1", "JUNK2"]
    eligible = {"AAPL", "MSFT"}
    result = _sync_save_snapshot(all_syms, "tradier_validated", eligible)

    assert result is True
    upsert_calls = sb.table.return_value.upsert.call_args_list
    inserted_symbols = []
    for c in upsert_calls:
        rows = c[0][0]
        inserted_symbols.extend(r["symbol"] for r in rows)
    assert set(inserted_symbols) == {"AAPL", "MSFT"}
    assert "JUNK1" not in inserted_symbols
    assert "JUNK2" not in inserted_symbols


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_symbol_count_matches_eligible(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    all_syms = ["AAPL", "MSFT", "JUNK1", "JUNK2", "JUNK3"]
    eligible = {"AAPL", "MSFT"}
    _sync_save_snapshot(all_syms, "tradier_validated", eligible)

    insert_call = sb.table.return_value.insert.call_args
    inserted_row = insert_call[0][0]
    assert inserted_row["symbol_count"] == 2  # not 5


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_no_null_rows_for_non_eligible(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    all_syms = ["AAPL", "MSFT", "LOW_VOL"]
    eligible = {"AAPL", "MSFT"}
    _sync_save_snapshot(all_syms, "tradier_validated", eligible)

    upsert_calls = sb.table.return_value.upsert.call_args_list
    all_rows = []
    for c in upsert_calls:
        all_rows.extend(c[0][0])
    # No row should have stream_eligible=False since we only insert eligible rows
    for row in all_rows:
        assert row["stream_eligible"] is True
        assert row["symbol"] != "LOW_VOL"


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_all_symbols_eligible_passthrough(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    syms = ["AAPL", "MSFT", "TSLA"]
    eligible = {"AAPL", "MSFT", "TSLA"}
    _sync_save_snapshot(syms, "tradier_validated", eligible)

    insert_call = sb.table.return_value.insert.call_args
    assert insert_call[0][0]["symbol_count"] == 3


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_empty_eligible_set_fallback(mock_client, mock_prune):
    """Empty eligible_set should fall back to writing all symbols."""
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    syms = ["AAPL", "MSFT"]
    result = _sync_save_snapshot(syms, "tradier_validated", set())  # empty set

    assert result is True
    upsert_calls = sb.table.return_value.upsert.call_args_list
    all_rows = []
    for c in upsert_calls:
        all_rows.extend(c[0][0])
    inserted = {r["symbol"] for r in all_rows}
    assert inserted == {"AAPL", "MSFT"}


@patch("services.universe_store._client")
def test_empty_symbols_guard(mock_client):
    from services.universe_store import _sync_save_snapshot
    result = _sync_save_snapshot([], "tradier_validated", set())
    assert result is False
    mock_client.assert_not_called()


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_upsert_idempotent_on_conflict(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    _sync_save_snapshot(["AAPL"], "tradier_validated", {"AAPL"})

    upsert_calls = sb.table.return_value.upsert.call_args_list
    assert len(upsert_calls) >= 1
    _, kwargs = upsert_calls[0]
    assert kwargs.get("on_conflict") == "snapshot_id,symbol"


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_snapshot_deactivation_called(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    _sync_save_snapshot(["AAPL"], "tradier_validated", {"AAPL"})

    update_calls = sb.table.return_value.update.call_args_list
    assert any(
        c[0][0] == {"is_active": False}
        for c in update_calls
    ), "Expected update({is_active: False}) to deactivate old snapshots"


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_prune_called_after_save(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    _sync_save_snapshot(["AAPL"], "tradier_validated", {"AAPL"})

    mock_prune.assert_called_once()


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_none_eligible_set_treated_as_all(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    syms = ["AAPL", "MSFT", "TSLA"]
    _sync_save_snapshot(syms, "tradier_validated", None)

    insert_call = sb.table.return_value.insert.call_args
    assert insert_call[0][0]["symbol_count"] == 3


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_rows_all_flagged_stream_eligible(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    syms = ["AAPL", "MSFT"]
    _sync_save_snapshot(syms, "tradier_validated", {"AAPL", "MSFT"})

    upsert_calls = sb.table.return_value.upsert.call_args_list
    for c in upsert_calls:
        for row in c[0][0]:
            assert row["stream_eligible"] is True


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_symbol_count_not_total_cboe_size(mock_client, mock_prune):
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    # Simulate 5270 CBOE symbols but only 4340 eligible
    all_syms = [f"SYM{i}" for i in range(5270)]
    eligible = {f"SYM{i}" for i in range(4340)}
    _sync_save_snapshot(all_syms, "tradier_validated", eligible)

    insert_call = sb.table.return_value.insert.call_args
    symbol_count = insert_call[0][0]["symbol_count"]
    assert symbol_count == 4340
    assert symbol_count != 5270


@patch("services.universe_store._prune_old_snapshots")
@patch("services.universe_store._client")
def test_batch_split_correct(mock_client, mock_prune):
    """600 eligible symbols should produce 2 upsert batches (500 + 100)."""
    sb = _make_sb_mock()
    mock_client.return_value = sb

    from services.universe_store import _sync_save_snapshot

    syms = [f"S{i}" for i in range(600)]
    eligible = set(syms)
    _sync_save_snapshot(syms, "tradier_validated", eligible)

    upsert_calls = sb.table.return_value.upsert.call_args_list
    batch_sizes = [len(c[0][0]) for c in upsert_calls]
    assert batch_sizes == [500, 100]


@patch("services.universe_store._client")
def test_exception_returns_false(mock_client):
    mock_client.side_effect = RuntimeError("DB connection failed")

    from services.universe_store import _sync_save_snapshot

    result = _sync_save_snapshot(["AAPL"], "tradier_validated", {"AAPL"})
    assert result is False
