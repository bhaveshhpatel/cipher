"""
Unit tests for services/chain_store.py

All Supabase calls are mocked — no live DB required.
Covers: save_chain, load_chain, empty inputs, error paths, pagination.

ING-008 additions:
  - get_contract_vol_oi: cache miss returns (None, None)
  - get_contract_vol_oi: cache hit returns correct (volume, open_interest)
  - invalidate_vol_oi_cache: clears the in-process cache
  - start_chain_refresh_worker: runs one cycle, populates cache, cancels cleanly
  - start_chain_refresh_worker: empty symbol list skips fetch_chain_fn call
  - start_chain_refresh_worker: fetch error on one symbol does not abort cycle
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import services.chain_store as chain_store_mod
from services.symbol_registry import ContractMeta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta(ticker="AAPL", strike=180.0, expiry="2026-01-17",
          ctype="CALL", dte=30, oi=500, tier=1) -> ContractMeta:
    return ContractMeta(
        ticker=ticker, strike=strike, expiry=expiry,
        contract_type=ctype, dte=dte, open_interest=oi, tier=tier,
    )


def _make_registry(n: int = 3) -> dict:
    return {
        f"AAPL  260117C0018{i:04d}": _meta(strike=180.0 + i)
        for i in range(n)
    }


def _mock_sb(rows=None, upsert_ok=True):
    """Return a mock Supabase client."""
    sb = MagicMock()
    # table().select().eq().range().execute().data
    exec_result = MagicMock()
    exec_result.data = rows if rows is not None else []
    (
        sb.table.return_value
          .select.return_value
          .eq.return_value
          .range.return_value
          .execute.return_value
    ) = exec_result
    if not upsert_ok:
        sb.table.return_value.upsert.side_effect = RuntimeError("DB error")
    else:
        sb.table.return_value.upsert.return_value.execute.return_value = MagicMock()
    return sb


# ---------------------------------------------------------------------------
# save_chain tests
# ---------------------------------------------------------------------------

def test_save_chain_success():
    registry = _make_registry(3)
    sb = _mock_sb(upsert_ok=True)
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(chain_store_mod.save_chain("snap-001", registry))
    assert result is True
    assert sb.table.called


def test_save_chain_empty_registry_returns_true():
    """Empty registry is a no-op and returns True without hitting DB."""
    with patch("services.chain_store._client") as mock_client:
        result = asyncio.run(chain_store_mod.save_chain("snap-001", {}))
    assert result is True
    mock_client.assert_not_called()


def test_save_chain_db_error_returns_false():
    sb = _mock_sb(upsert_ok=False)
    registry = _make_registry(2)
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(chain_store_mod.save_chain("snap-001", registry))
    assert result is False


def test_save_chain_batches_correctly():
    """601 rows → 2 upsert calls (batch size 500)."""
    registry = _make_registry(601)
    sb = _mock_sb(upsert_ok=True)
    with patch("services.chain_store._client", return_value=sb):
        asyncio.run(chain_store_mod.save_chain("snap-002", registry))
    assert sb.table.return_value.upsert.call_count == 2


def test_save_chain_missing_service_key_returns_false():
    """_client() raises RuntimeError when SUPABASE_SERVICE_KEY is empty."""
    with patch("services.chain_store.settings") as mock_settings:
        mock_settings.SUPABASE_SERVICE_KEY = ""
        mock_settings.SUPABASE_URL = "https://x.supabase.co"
        result = asyncio.run(chain_store_mod.save_chain("snap-001", _make_registry(1)))
    assert result is False   # RuntimeError caught → False


# ---------------------------------------------------------------------------
# load_chain tests
# ---------------------------------------------------------------------------

def _make_db_rows(n: int = 3) -> list[dict]:
    return [
        {
            "occ_symbol":    f"AAPL  260117C0018{i:04d}",
            "ticker":        "AAPL",
            "contract_type": "CALL",
            "strike":        180.0 + i,
            "expiry":        "2026-01-17",
            "dte":           30,
            "open_interest": 500,
            "tier":          1,
        }
        for i in range(n)
    ]


def test_load_chain_returns_contract_meta():
    rows = _make_db_rows(3)
    sb = _mock_sb(rows=rows)
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(chain_store_mod.load_chain("snap-001"))
    assert result is not None
    assert len(result) == 3
    first_key = list(result.keys())[0]
    assert isinstance(result[first_key], ContractMeta)
    assert result[first_key].ticker == "AAPL"


def test_load_chain_empty_table_returns_empty_dict():
    sb = _mock_sb(rows=[])
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(chain_store_mod.load_chain("snap-001"))
    assert result == {}


def test_load_chain_db_error_returns_none():
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("connection refused")
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(chain_store_mod.load_chain("snap-001"))
    assert result is None


def test_load_chain_skips_rows_with_empty_occ_symbol():
    rows = _make_db_rows(2)
    rows.append({"occ_symbol": "", "ticker": "SPY", "contract_type": "PUT",
                 "strike": 500.0, "expiry": "2026-01-17",
                 "dte": 10, "open_interest": 100, "tier": 2})
    sb = _mock_sb(rows=rows)
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(chain_store_mod.load_chain("snap-001"))
    assert len(result) == 2   # blank occ_symbol row is skipped


def test_load_chain_tier_defaults_to_3_when_missing():
    rows = [{
        "occ_symbol": "SPY   260117C00500000",
        "ticker": "SPY",
        "contract_type": "CALL",
        "strike": 500.0,
        "expiry": "2026-01-17",
        "dte": 10,
        "open_interest": 200,
        "tier": None,   # NULL in DB
    }]
    sb = _mock_sb(rows=rows)
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(chain_store_mod.load_chain("snap-001"))
    assert result["SPY   260117C00500000"].tier == 3


# ---------------------------------------------------------------------------
# ING-008: get_contract_vol_oi
# ---------------------------------------------------------------------------

def test_get_contract_vol_oi_cache_miss_returns_none_tuple():
    """Cache miss for an OCC symbol returns (None, None) — no API call."""
    chain_store_mod._vol_oi_cache.clear()
    vol, oi = chain_store_mod.get_contract_vol_oi("AAPL240620C00180000")
    assert vol is None
    assert oi is None


def test_get_contract_vol_oi_cache_hit_returns_correct_values():
    """Cache hit returns the populated (volume, open_interest) tuple."""
    import time
    chain_store_mod._vol_oi_cache["AAPL240620C00180000"] = {
        "volume": 1234,
        "open_interest": 5678,
        "refreshed_at": time.time(),
    }
    vol, oi = chain_store_mod.get_contract_vol_oi("AAPL240620C00180000")
    assert vol == 1234
    assert oi == 5678
    chain_store_mod._vol_oi_cache.pop("AAPL240620C00180000", None)


def test_get_contract_vol_oi_partial_cache_entry():
    """If volume or oi is missing from the cache entry, returns None for that field."""
    import time
    chain_store_mod._vol_oi_cache["SPY240517P00500000"] = {
        "volume": 42,
        "refreshed_at": time.time(),
        # open_interest intentionally absent
    }
    vol, oi = chain_store_mod.get_contract_vol_oi("SPY240517P00500000")
    assert vol == 42
    assert oi is None
    chain_store_mod._vol_oi_cache.pop("SPY240517P00500000", None)


# ---------------------------------------------------------------------------
# ING-008: invalidate_vol_oi_cache
# ---------------------------------------------------------------------------

def test_invalidate_vol_oi_cache_clears_all_entries():
    """invalidate_vol_oi_cache() wipes the entire _vol_oi_cache dict."""
    import time
    chain_store_mod._vol_oi_cache["AAPL240620C00180000"] = {
        "volume": 100, "open_interest": 200, "refreshed_at": time.time()
    }
    chain_store_mod._vol_oi_cache["TSLA240620C00200000"] = {
        "volume": 50, "open_interest": 300, "refreshed_at": time.time()
    }
    assert len(chain_store_mod._vol_oi_cache) >= 2
    chain_store_mod.invalidate_vol_oi_cache()
    assert len(chain_store_mod._vol_oi_cache) == 0


# ---------------------------------------------------------------------------
# ING-008: start_chain_refresh_worker
# ---------------------------------------------------------------------------

def test_start_chain_refresh_worker_populates_cache():
    """
    Worker runs one refresh cycle, populates _vol_oi_cache, then cancels cleanly.
    fetch_chain_fn returns two contract dicts; both should appear in the cache.
    """
    chain_store_mod._vol_oi_cache.clear()

    contracts = [
        {"symbol": "AAPL240620C00180000", "volume": 500, "open_interest": 3000},
        {"symbol": "AAPL240620P00180000", "volume": 200, "open_interest": 1500},
    ]

    async def fake_fetch(symbol: str) -> list:
        return contracts if symbol == "AAPL" else []

    # Patch the interval to 0 so the worker fires immediately on first sleep.
    with patch("services.chain_store._CHAIN_REFRESH_INTERVAL_S", 0):
        async def _run():
            task = asyncio.create_task(
                chain_store_mod.start_chain_refresh_worker(
                    get_tracked_symbols=lambda: ["AAPL"],
                    fetch_chain_fn=fake_fetch,
                )
            )
            # Give the worker enough time to complete one sleep(0) + one cycle.
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())

    assert "AAPL240620C00180000" in chain_store_mod._vol_oi_cache
    assert chain_store_mod._vol_oi_cache["AAPL240620C00180000"]["volume"] == 500
    assert chain_store_mod._vol_oi_cache["AAPL240620C00180000"]["open_interest"] == 3000
    assert "AAPL240620P00180000" in chain_store_mod._vol_oi_cache
    chain_store_mod._vol_oi_cache.clear()


def test_start_chain_refresh_worker_empty_symbols_skips_fetch():
    """
    When get_tracked_symbols() returns [], the worker must skip the cycle
    and NOT call fetch_chain_fn.
    """
    fetch_calls = []

    async def fetch_should_not_be_called(symbol: str) -> list:
        fetch_calls.append(symbol)
        return []

    with patch("services.chain_store._CHAIN_REFRESH_INTERVAL_S", 0):
        async def _run():
            task = asyncio.create_task(
                chain_store_mod.start_chain_refresh_worker(
                    get_tracked_symbols=lambda: [],
                    fetch_chain_fn=fetch_should_not_be_called,
                )
            )
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())

    assert fetch_calls == [], (
        "fetch_chain_fn must not be called when symbol list is empty"
    )


def test_start_chain_refresh_worker_error_on_one_symbol_does_not_abort():
    """
    If fetch_chain_fn raises for one symbol, the worker must continue to
    the next symbol and still populate cache entries for the good symbol.
    """
    chain_store_mod._vol_oi_cache.clear()

    async def flaky_fetch(symbol: str) -> list:
        if symbol == "BAD":
            raise RuntimeError("network error")
        return [{"symbol": "NVDA240620C00900000", "volume": 77, "open_interest": 444}]

    with patch("services.chain_store._CHAIN_REFRESH_INTERVAL_S", 0):
        async def _run():
            task = asyncio.create_task(
                chain_store_mod.start_chain_refresh_worker(
                    get_tracked_symbols=lambda: ["BAD", "NVDA"],
                    fetch_chain_fn=flaky_fetch,
                )
            )
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())

    # BAD symbol errored but NVDA's contract should still be in the cache.
    assert "NVDA240620C00900000" in chain_store_mod._vol_oi_cache, (
        "Cache must be populated for symbols that succeed even when another symbol errors"
    )
    chain_store_mod._vol_oi_cache.clear()
