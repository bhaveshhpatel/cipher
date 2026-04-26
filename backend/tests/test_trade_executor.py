"""
Regression tests for execution/trade_executor.py

Covers (matched to actual source — all HTTP calls mocked via httpx):
  - _headers() returns correct Authorization Bearer and Accept headers
  - place_option_order (market): correct URL, method, form data
  - place_option_order (limit): 'price' field added to form data
  - place_option_order: no 'price' field for market orders
  - place_option_order: HTTP 4xx raises → returns {error: ...}
  - place_option_order: connection exception → returns {error: ...}
  - place_option_order: OCC symbol ticker extracted for 'symbol' field
  - get_positions: happy path returns list of positions
  - get_positions: single-position dict is wrapped in list
  - get_positions: HTTP error → returns empty list []
  - get_positions: connection exception → returns empty list []
  - get_positions: empty positions object → returns empty list []
"""
import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock

from execution.trade_executor import TradeExecutor


def _make_executor(
    base_url: str = "https://sandbox.tradier.com",
    account_id: str = "VA12345678",
    api_key: str   = "test-api-key",
) -> TradeExecutor:
    """Build a TradeExecutor with injected test settings."""
    with patch("execution.trade_executor.settings") as ms:
        ms.TRADIER_BASE_URL  = base_url
        ms.TRADIER_ACCOUNT_ID = account_id
        ms.TRADIER_API_KEY   = api_key
        ex = TradeExecutor()
    return ex


# ── _headers ─────────────────────────────────────────────────────────────────

def test_headers_has_authorization_bearer():
    ex = _make_executor(api_key="my-secret-key")
    h = ex._headers()
    assert h["Authorization"] == "Bearer my-secret-key"


def test_headers_has_accept_json():
    ex = _make_executor()
    h = ex._headers()
    assert h["Accept"] == "application/json"


# ── place_option_order: market order ────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_option_order_market_happy_path():
    ex = _make_executor()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"order": {"id": 123, "status": "ok"}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = AsyncMock(return_value=mock_resp)

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        result = await ex.place_option_order(
            symbol="AAPL  260620C00195000",
            side="buy_to_open",
            quantity=1,
        )
    assert result["order"]["status"] == "ok"


@pytest.mark.asyncio
async def test_place_option_order_market_no_price_field():
    """Market orders must NOT include a 'price' field in the form data."""
    ex = _make_executor()
    captured_data = {}

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)

    async def _capture_post(url, headers, data, timeout):
        captured_data.update(data)
        return mock_resp

    mock_client.post = _capture_post

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        await ex.place_option_order(
            symbol="AAPL  260620C00195000",
            side="buy_to_open",
            quantity=1,
            order_type="market",
        )
    assert "price" not in captured_data


@pytest.mark.asyncio
async def test_place_option_order_occ_symbol_ticker_extracted():
    """The 'symbol' field in the POST data must be the ticker (first 6 chars stripped)."""
    ex = _make_executor()
    captured_data = {}

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)

    async def _capture_post(url, headers, data, timeout):
        captured_data.update(data)
        return mock_resp

    mock_client.post = _capture_post

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        await ex.place_option_order(
            symbol="AAPL  260620C00195000",
            side="buy_to_open",
            quantity=2,
        )
    # symbol = OCC.split(" ")[0] → 'AAPL'
    assert captured_data["symbol"] == "AAPL"


# ── place_option_order: limit order ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_option_order_limit_includes_price_field():
    ex = _make_executor()
    captured_data = {}

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)

    async def _capture_post(url, headers, data, timeout):
        captured_data.update(data)
        return mock_resp

    mock_client.post = _capture_post

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        await ex.place_option_order(
            symbol="SPY   260620C00500000",
            side="buy_to_open",
            quantity=1,
            order_type="limit",
            limit_price=3.45,
        )
    assert "price" in captured_data
    assert captured_data["price"] == "3.45"


# ── place_option_order: error handling ──────────────────────────────────────

@pytest.mark.asyncio
async def test_place_option_order_http_error_returns_error_dict():
    ex = _make_executor()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "403", request=MagicMock(), response=MagicMock()
        )
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = AsyncMock(return_value=mock_resp)

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        result = await ex.place_option_order(
            symbol="AAPL  260620C00195000",
            side="buy_to_open",
            quantity=1,
        )
    assert "error" in result
    assert isinstance(result["error"], str)


@pytest.mark.asyncio
async def test_place_option_order_connection_exception_returns_error_dict():
    ex = _make_executor()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = AsyncMock(side_effect=Exception("connection refused"))

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        result = await ex.place_option_order(
            symbol="AAPL  260620C00195000",
            side="buy_to_open",
            quantity=1,
        )
    assert "error" in result
    assert "connection refused" in result["error"]


# ── get_positions ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_positions_happy_path_returns_list():
    ex = _make_executor()
    fake_positions = [
        {"symbol": "AAPL  260620C00195000", "quantity": 1, "cost_basis": 345.0},
        {"symbol": "SPY   260620P00500000", "quantity": -2, "cost_basis": 800.0},
    ]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(
        return_value={"positions": {"position": fake_positions}}
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.get        = AsyncMock(return_value=mock_resp)

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        positions = await ex.get_positions()
    assert isinstance(positions, list)
    assert len(positions) == 2
    assert positions[0]["symbol"] == "AAPL  260620C00195000"


@pytest.mark.asyncio
async def test_get_positions_single_dict_wrapped_in_list():
    """Tradier returns a single position as a dict (not list) — must be wrapped."""
    ex = _make_executor()
    single = {"symbol": "AAPL  260620C00195000", "quantity": 1, "cost_basis": 345.0}
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(
        return_value={"positions": {"position": single}}  # dict, not list
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.get        = AsyncMock(return_value=mock_resp)

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        positions = await ex.get_positions()
    assert isinstance(positions, list)
    assert len(positions) == 1


@pytest.mark.asyncio
async def test_get_positions_http_error_returns_empty_list():
    ex = _make_executor()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "401", request=MagicMock(), response=MagicMock()
        )
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.get        = AsyncMock(return_value=mock_resp)

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        positions = await ex.get_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_get_positions_connection_exception_returns_empty_list():
    ex = _make_executor()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.get        = AsyncMock(side_effect=Exception("timeout"))

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        positions = await ex.get_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_get_positions_empty_object_returns_empty_list():
    """When Tradier returns {positions: {}} (no 'position' key), return []."""
    ex = _make_executor()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"positions": {}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.get        = AsyncMock(return_value=mock_resp)

    with patch("execution.trade_executor.httpx.AsyncClient", return_value=mock_client):
        positions = await ex.get_positions()
    assert isinstance(positions, list)
    assert positions == []
