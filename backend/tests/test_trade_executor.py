"""
Regression tests for execution/trade_executor.py

Strategy:
  - httpx.AsyncClient patched via unittest.mock so no real HTTP calls.
  - settings patched for all tests to provide deterministic base_url/
    account_id/api_key values.
  - All tests are async (pytest-asyncio).

Covers:
  TradeExecutor.__init__:
  - Reads base_url from settings.TRADIER_BASE_URL
  - Reads account_id from settings.TRADIER_ACCOUNT_ID
  - Reads api_key from settings.TRADIER_API_KEY

  _headers:
  - Returns {'Authorization': 'Bearer <key>', 'Accept': 'application/json'}

  place_option_order:
  - Market order: POSTs to correct URL with correct payload
  - Limit order: adds 'price' field when order_type='limit' + limit_price set
  - Limit order without limit_price: 'price' field NOT added
  - symbol root extracted correctly (splits OCC symbol on space)
  - HTTP success (200): returns parsed JSON dict
  - HTTP error (4xx/5xx, raise_for_status raises): returns {error: str}
  - Network exception (httpx.ConnectError): returns {error: str}, no raise

  get_positions:
  - HTTP success: returns list of position dicts
  - Single position dict (not list): wrapped in list
  - Empty positions key: returns []
  - Network exception: returns [], no raise
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import httpx

from execution.trade_executor import TradeExecutor


# ---------------------------------------------------------------------------
# Settings patch helper
# ---------------------------------------------------------------------------

SETTINGS_PATCH = {
    "TRADIER_BASE_URL":   "https://sandbox.tradier.com",
    "TRADIER_ACCOUNT_ID": "ACC123",
    "TRADIER_API_KEY":    "test_api_key",
}


def _patched_executor() -> TradeExecutor:
    """Return a TradeExecutor with deterministic settings."""
    with patch("execution.trade_executor.settings") as ms:
        for k, v in SETTINGS_PATCH.items():
            setattr(ms, k, v)
        return TradeExecutor()


# ---------------------------------------------------------------------------
# __init__ and _headers
# ---------------------------------------------------------------------------

def test_init_reads_base_url():
    ex = _patched_executor()
    assert ex.base_url == "https://sandbox.tradier.com"


def test_init_reads_account_id():
    ex = _patched_executor()
    assert ex.account_id == "ACC123"


def test_init_reads_api_key():
    ex = _patched_executor()
    assert ex.api_key == "test_api_key"


def test_headers_authorization_format():
    ex = _patched_executor()
    h = ex._headers()
    assert h["Authorization"] == "Bearer test_api_key"


def test_headers_accept_json():
    ex = _patched_executor()
    assert ex._headers()["Accept"] == "application/json"


# ---------------------------------------------------------------------------
# place_option_order helpers
# ---------------------------------------------------------------------------

def _make_mock_response(json_data: dict, status_code: int = 200):
    mock_resp = MagicMock()
    mock_resp.json.return_value = json_data
    mock_resp.status_code = status_code
    mock_resp.raise_for_status = MagicMock()  # no-op by default
    return mock_resp


def _make_error_response():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "422 Unprocessable",
        request=MagicMock(),
        response=MagicMock(),
    )
    return mock_resp


# ---------------------------------------------------------------------------
# place_option_order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_place_order_posts_to_correct_url():
    ex = _patched_executor()
    captured = {}

    async def _fake_post(url, **kwargs):
        captured["url"] = url
        return _make_mock_response({"order": {"id": 1}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(side_effect=_fake_post)
        MockClient.return_value = mock_ctx
        await ex.place_option_order("AAPL 260C", "buy_to_open", 1)

    assert "ACC123" in captured["url"]
    assert "orders" in captured["url"]


@pytest.mark.asyncio
async def test_place_order_market_payload_fields():
    ex = _patched_executor()
    captured = {}

    async def _fake_post(url, **kwargs):
        captured["data"] = kwargs.get("data", {})
        return _make_mock_response({"order": {"id": 2}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(side_effect=_fake_post)
        MockClient.return_value = mock_ctx
        await ex.place_option_order("AAPL 260C", "buy_to_open", 2)

    data = captured["data"]
    assert data["class"]         == "option"
    assert data["side"]          == "buy_to_open"
    assert data["quantity"]      == "2"
    assert data["type"]          == "market"
    assert data["duration"]      == "day"
    assert data["option_symbol"] == "AAPL 260C"


@pytest.mark.asyncio
async def test_place_order_symbol_root_extracted():
    ex = _patched_executor()
    captured = {}

    async def _fake_post(url, **kwargs):
        captured["data"] = kwargs.get("data", {})
        return _make_mock_response({"order": {"id": 3}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(side_effect=_fake_post)
        MockClient.return_value = mock_ctx
        await ex.place_option_order("AAPL 260C", "buy_to_open", 1)

    assert captured["data"]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_place_order_limit_adds_price_field():
    ex = _patched_executor()
    captured = {}

    async def _fake_post(url, **kwargs):
        captured["data"] = kwargs.get("data", {})
        return _make_mock_response({"order": {"id": 4}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(side_effect=_fake_post)
        MockClient.return_value = mock_ctx
        await ex.place_option_order(
            "AAPL 260C", "buy_to_open", 1,
            order_type="limit", limit_price=3.50,
        )

    assert captured["data"]["price"] == "3.5"


@pytest.mark.asyncio
async def test_place_order_limit_without_price_no_price_field():
    ex = _patched_executor()
    captured = {}

    async def _fake_post(url, **kwargs):
        captured["data"] = kwargs.get("data", {})
        return _make_mock_response({"order": {"id": 5}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(side_effect=_fake_post)
        MockClient.return_value = mock_ctx
        await ex.place_option_order(
            "AAPL 260C", "buy_to_open", 1,
            order_type="limit",  # no limit_price
        )

    assert "price" not in captured["data"]


@pytest.mark.asyncio
async def test_place_order_returns_json_on_success():
    ex = _patched_executor()
    expected = {"order": {"id": 999, "status": "ok"}}

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(return_value=_make_mock_response(expected))
        MockClient.return_value = mock_ctx
        result = await ex.place_option_order("AAPL 260C", "buy_to_open", 1)

    assert result == expected


@pytest.mark.asyncio
async def test_place_order_http_error_returns_error_dict():
    ex = _patched_executor()

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(return_value=_make_error_response())
        MockClient.return_value = mock_ctx
        result = await ex.place_option_order("AAPL 260C", "buy_to_open", 1)

    assert "error" in result


@pytest.mark.asyncio
async def test_place_order_network_exception_returns_error_dict():
    ex = _patched_executor()

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(side_effect=Exception("connection refused"))
        MockClient.return_value = mock_ctx
        result = await ex.place_option_order("AAPL 260C", "buy_to_open", 1)

    assert "error" in result
    assert "connection refused" in result["error"]


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_positions_returns_list():
    ex = _patched_executor()
    positions_data = [
        {"symbol": "AAPL", "quantity": 1},
        {"symbol": "TSLA", "quantity": -1},
    ]
    resp = _make_mock_response({"positions": {"position": positions_data}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=resp)
        MockClient.return_value = mock_ctx
        result = await ex.get_positions()

    assert result == positions_data


@pytest.mark.asyncio
async def test_get_positions_single_dict_wrapped_in_list():
    """Tradier returns a dict (not a list) when there is exactly 1 position."""
    ex = _patched_executor()
    single = {"symbol": "NVDA", "quantity": 2}
    resp   = _make_mock_response({"positions": {"position": single}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=resp)
        MockClient.return_value = mock_ctx
        result = await ex.get_positions()

    assert isinstance(result, list)
    assert result[0] == single


@pytest.mark.asyncio
async def test_get_positions_empty_returns_empty_list():
    ex   = _patched_executor()
    resp = _make_mock_response({"positions": {}})

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=resp)
        MockClient.return_value = mock_ctx
        result = await ex.get_positions()

    assert result == []


@pytest.mark.asyncio
async def test_get_positions_exception_returns_empty_list():
    ex = _patched_executor()

    with patch("httpx.AsyncClient") as MockClient:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(side_effect=Exception("timeout"))
        MockClient.return_value = mock_ctx
        result = await ex.get_positions()

    assert result == []
