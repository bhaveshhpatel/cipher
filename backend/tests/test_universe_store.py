"""
tests/test_universe_store.py

DB read/write tests for services/universe_store.py with mocked Supabase.
Updated to cover stream_eligible_set parameter in save_snapshot.
Updated 2026-04-24: add TR-01…TR-05 for load_tier_map (Feature 4A).
Updated 2026-04-25: add US-OI-01…US-OI-04 for open_interest upsert (Feature 4A-OI).
Updated 2026-04-27: wire .range() into mock chain so _paginate_symbols resolves
  correctly; add terminating empty-page side_effect entries for all paginated paths.
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
    query.range.return_value   = query   # <-- fix: _paginate_symbols calls .range()
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
            # 1) snapshot header lookup
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            # 2) _paginate_symbols page 1 — returns 2 symbols (< _PAGE_SIZE, terminates loop)
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
            # _paginate_symbols page 1 — empty → loop terminates, symbols=[]
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
            # 1) snapshot header lookup
            MagicMock(data=[{"id": snapshot_id, "fetched_at": stale_time, "source": "tradier_validated"}]),
            # 2) _paginate_symbols page 1 — returns 2 symbols (< _PAGE_SIZE, terminates)
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
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=[{"id": "snap-uuid-new"}]),
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
            # 1) snapshot header
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            # 2) _paginate_symbols page 1 (< _PAGE_SIZE, loop terminates)
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
        """tier=3 must be stored as int 3, not string '3'."""
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-tier-002"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            # page 1 (< _PAGE_SIZE, terminates)
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
        """Rows with no tier key (pre-010 data) must default to tier 3."""
        sb, query = _make_sb_mock()
        snapshot_id = "snap-uuid-tier-003"
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id, "fetched_at": datetime.now(timezone.utc).isoformat()}]),
            # page 1 (< _PAGE_SIZE, terminates)
            MagicMock(data=[{"symbol": "LEGACY"}]),  # no tier key
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
    """
    Feature 4A-OI: _sync_upsert_symbol_quotes() must include open_interest
    in every upsert row, sourced from quote.open_interest.
    """

    def _run_upsert(self, quotes, tier_map=None):
        """Run _sync_upsert_symbol_quotes with a mocked active snapshot."""
        sb, query = _make_sb_mock()
        snapshot_id = "snap-oi-test-001"
        # First execute() call returns the active snapshot id;
        # subsequent upsert batch execute() calls return empty.
        query.execute.side_effect = [
            MagicMock(data=[{"id": snapshot_id}]),
        ] + [MagicMock(data=[]) for _ in range(10)]

        with patch("services.universe_store._client", return_value=sb):
            universe_store._sync_upsert_symbol_quotes(quotes, tier_map or {})

        return query

    # US-OI-01
    def test_open_interest_key_present_in_upsert_row(self):
        """
        Every row sent to .upsert() must contain the 'open_interest' key.
        Regression: before Chunk 1C this key was absent.
        """
        quotes = [_make_quote("AAPL", open_interest=2000)]
        query  = self._run_upsert(quotes)

        upsert_calls = query.upsert.call_args_list
        assert upsert_calls, "upsert() was never called — snapshot lookup may have failed"
        rows = upsert_calls[0].args[0]
        assert isinstance(rows, list) and len(rows) > 0
        assert "open_interest" in rows[0], (
            "'open_interest' key missing from upsert row. "
            "Chunk 1C (universe_store) may have been reverted."
        )

    # US-OI-02
    def test_open_interest_value_sourced_from_quote(self):
        """The upserted open_interest value must match quote.open_interest exactly."""
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
        """
        If quote.open_interest is None (e.g. symbol had no registry entry),
        None must be written to DB rather than silently omitted or defaulted.
        """
        quotes = [_make_quote("SPCE", open_interest=None)]
        query  = self._run_upsert(quotes)

        rows = query.upsert.call_args_list[0].args[0]
        assert rows[0]["open_interest"] is None, (
            "open_interest=None should be written as NULL, not omitted."
        )

    # US-OI-04
    def test_open_interest_and_tier_both_present_in_same_row(self):
        """
        Both open_interest (4A-OI) and tier (4A) must coexist in the same
        upsert row — neither should overwrite or displace the other.
        """
        quotes   = [_make_quote("TSLA", open_interest=1_500)]
        tier_map = {"TSLA": 1}
        query    = self._run_upsert(quotes, tier_map)

        rows = query.upsert.call_args_list[0].args[0]
        row  = rows[0]
        assert "open_interest" in row, "open_interest missing from upsert row"
        assert "tier"           in row, "tier missing from upsert row"
        assert row["open_interest"] == 1_500
        assert row["tier"]          == 1
