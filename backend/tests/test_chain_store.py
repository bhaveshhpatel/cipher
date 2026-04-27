"""
Unit tests for services/chain_store.py

All Supabase calls are mocked — no live DB required.
Covers: save_chain, load_chain, empty inputs, error paths, pagination.
"""
import asyncio
from unittest.mock import MagicMock, patch

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
    # table().upsert().execute()
    upsert_exec = MagicMock()
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
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["save_chain"]).save_chain(
                "snap-001", registry
            )
        )
    assert result is True
    assert sb.table.called


def test_save_chain_empty_registry_returns_true():
    """Empty registry is a no-op and returns True without hitting DB."""
    with patch("services.chain_store._client") as mock_client:
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["save_chain"]).save_chain(
                "snap-001", {}
            )
        )
    assert result is True
    mock_client.assert_not_called()


def test_save_chain_db_error_returns_false():
    sb = _mock_sb(upsert_ok=False)
    registry = _make_registry(2)
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["save_chain"]).save_chain(
                "snap-001", registry
            )
        )
    assert result is False


def test_save_chain_batches_correctly():
    """601 rows → 2 upsert calls (batch size 500)."""
    registry = _make_registry(601)
    sb = _mock_sb(upsert_ok=True)
    with patch("services.chain_store._client", return_value=sb):
        asyncio.run(
            __import__("services.chain_store", fromlist=["save_chain"]).save_chain(
                "snap-002", registry
            )
        )
    assert sb.table.return_value.upsert.call_count == 2


def test_save_chain_missing_service_key_raises():
    """_client() raises RuntimeError when SUPABASE_SERVICE_KEY is empty."""
    with patch("services.chain_store.settings") as mock_settings:
        mock_settings.SUPABASE_SERVICE_KEY = ""
        mock_settings.SUPABASE_URL = "https://x.supabase.co"
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["save_chain"]).save_chain(
                "snap-001", _make_registry(1)
            )
        )
    assert result is False   # RuntimeError caught, returns False


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
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["load_chain"]).load_chain(
                "snap-001"
            )
        )
    assert result is not None
    assert len(result) == 3
    first_key = list(result.keys())[0]
    assert isinstance(result[first_key], ContractMeta)
    assert result[first_key].ticker == "AAPL"


def test_load_chain_empty_table_returns_empty_dict():
    sb = _mock_sb(rows=[])
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["load_chain"]).load_chain(
                "snap-001"
            )
        )
    assert result == {}


def test_load_chain_db_error_returns_none():
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("connection refused")
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["load_chain"]).load_chain(
                "snap-001"
            )
        )
    assert result is None


def test_load_chain_skips_rows_with_empty_occ_symbol():
    rows = _make_db_rows(2)
    rows.append({"occ_symbol": "", "ticker": "SPY", "contract_type": "PUT",
                 "strike": 500.0, "expiry": "2026-01-17",
                 "dte": 10, "open_interest": 100, "tier": 2})
    sb = _mock_sb(rows=rows)
    with patch("services.chain_store._client", return_value=sb):
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["load_chain"]).load_chain(
                "snap-001"
            )
        )
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
        result = asyncio.run(
            __import__("services.chain_store", fromlist=["load_chain"]).load_chain(
                "snap-001"
            )
        )
    assert result["SPY   260117C00500000"].tier == 3
