"""
Phase 4 — test_trade_executor.py

Covers every branch in execution/trade_executor.py:
  - place_option_order(): success path → returns API response dict
  - place_option_order(): market order (no price field in payload)
  - place_option_order(): limit order → price field included at 2dp
  - place_option_order(): limit_price ignored when order_type != 'limit'
  - place_option_order(): HTTP 4xx raises → returns {"error": ...}
  - place_option_order(): network exception → returns {"error": ...}
  - place_option_order(): payload shape (class, symbol, option_symbol, side, quantity, type, duration)
  - place_option_order(): OCC symbol with spaces → root symbol extracted correctly
  - get_positions(): success path → returns list of positions
  - get_positions(): single position dict → wrapped in list
  - get_positions(): positions key missing → returns []
  - get_positions(): HTTP error → returns []
  - get_positions(): network exception → returns []
  - _headers(): Authorization Bearer format
"""
import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_response(status_code: int, body: dict) -> MagicMock:
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.json.return_value = body
    if status_code >= 400:
        mock.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=mock,
        )
    else:
        mock.raise_for_status.return_value = None
    return mock


def _mock_settings():
    s = MagicMock()
    s.TRADIER_BASE_URL   = "https://sandbox.tradier.com"
    s.TRADIER_ACCOUNT_ID = "VA12345678"
    s.TRADIER_API_KEY    = "test-api-key"
    return s


class TestTradeExecutorPlaceOrder:

    def _executor(self):
        with patch("execution.trade_executor.settings", _mock_settings()):
            from execution.trade_executor import TradeExecutor
            return TradeExecutor()

    def test_success_returns_api_response(self):
        executor = self._executor()
        ok_body  = {"order": {"id": 12345, "status": "ok", "partner_id": 3}}
        mock_resp = _make_response(200, ok_body)
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.place_option_order(
                symbol="AAPL  260620C00200000",
                side="buy_to_open",
                quantity=5,
            ))
        assert result == ok_body

    def test_market_order_no_price_field(self):
        executor   = self._executor()
        captured   = {}
        mock_resp  = _make_response(200, {"order": {"id": 1}})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        async def _post(url, headers, data, timeout):
            captured.update(data)
            return mock_resp
        mock_client.post = _post
        with patch("httpx.AsyncClient", return_value=mock_client):
            _run(executor.place_option_order(
                symbol="AAPL  260620C00200000",
                side="buy_to_open",
                quantity=1,
                order_type="market",
            ))
        assert "price" not in captured
        assert captured["type"] == "market"

    def test_limit_order_includes_price_at_2dp(self):
        executor   = self._executor()
        captured   = {}
        mock_resp  = _make_response(200, {"order": {"id": 2}})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        async def _post(url, headers, data, timeout):
            captured.update(data)
            return mock_resp
        mock_client.post = _post
        with patch("httpx.AsyncClient", return_value=mock_client):
            _run(executor.place_option_order(
                symbol="AAPL  260620C00200000",
                side="buy_to_open",
                quantity=2,
                order_type="limit",
                limit_price=3.456,
            ))
        assert "price" in captured
        assert captured["price"] == "3.46"

    def test_limit_price_ignored_when_order_type_market(self):
        executor   = self._executor()
        captured   = {}
        mock_resp  = _make_response(200, {"order": {"id": 3}})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        async def _post(url, headers, data, timeout):
            captured.update(data)
            return mock_resp
        mock_client.post = _post
        with patch("httpx.AsyncClient", return_value=mock_client):
            _run(executor.place_option_order(
                symbol="AAPL  260620C00200000",
                side="buy_to_open",
                quantity=1,
                order_type="market",
                limit_price=9.99,  # should be ignored
            ))
        assert "price" not in captured

    def test_http_error_returns_error_dict(self):
        executor   = self._executor()
        mock_resp  = _make_response(401, {})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.place_option_order(
                symbol="AAPL  260620C00200000",
                side="buy_to_open",
                quantity=1,
            ))
        assert "error" in result

    def test_network_exception_returns_error_dict(self):
        executor   = self._executor()
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(side_effect=httpx.ConnectError("timeout"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.place_option_order(
                symbol="AAPL  260620C00200000",
                side="buy_to_open",
                quantity=1,
            ))
        assert "error" in result

    def test_payload_shape(self):
        executor   = self._executor()
        captured   = {}
        mock_resp  = _make_response(200, {"order": {"id": 4}})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        async def _post(url, headers, data, timeout):
            captured.update(data)
            return mock_resp
        mock_client.post = _post
        with patch("httpx.AsyncClient", return_value=mock_client):
            _run(executor.place_option_order(
                symbol="TSLA  260620P00250000",
                side="sell_to_close",
                quantity=3,
                duration="gtc",
            ))
        assert captured["class"]         == "option"
        assert captured["symbol"]        == "TSLA"
        assert captured["option_symbol"] == "TSLA  260620P00250000"
        assert captured["side"]          == "sell_to_close"
        assert captured["quantity"]      == "3"
        assert captured["duration"]      == "gtc"

    def test_occ_symbol_root_extracted_on_space(self):
        executor   = self._executor()
        captured   = {}
        mock_resp  = _make_response(200, {"order": {"id": 5}})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        async def _post(url, headers, data, timeout):
            captured.update(data)
            return mock_resp
        mock_client.post = _post
        with patch("httpx.AsyncClient", return_value=mock_client):
            _run(executor.place_option_order(
                symbol="NVDA  260918C01000000",
                side="buy_to_open",
                quantity=1,
            ))
        # split(" ")[0] → "NVDA"
        assert captured["symbol"] == "NVDA"


class TestTradeExecutorGetPositions:

    def _executor(self):
        with patch("execution.trade_executor.settings", _mock_settings()):
            from execution.trade_executor import TradeExecutor
            return TradeExecutor()

    def test_success_returns_list(self):
        executor  = self._executor()
        positions = [{"symbol": "AAPL", "quantity": 2, "cost_basis": 400}]
        body      = {"positions": {"position": positions}}
        mock_resp = _make_response(200, body)
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.get        = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.get_positions())
        assert result == positions

    def test_single_position_dict_wrapped_in_list(self):
        executor  = self._executor()
        single_pos = {"symbol": "TSLA", "quantity": 1, "cost_basis": 200}
        body       = {"positions": {"position": single_pos}}  # dict, not list
        mock_resp  = _make_response(200, body)
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.get        = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.get_positions())
        assert isinstance(result, list)
        assert result[0] == single_pos

    def test_positions_key_missing_returns_empty_list(self):
        executor  = self._executor()
        body      = {}  # no 'positions' key
        mock_resp = _make_response(200, body)
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.get        = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.get_positions())
        assert result == []

    def test_http_error_returns_empty_list(self):
        executor  = self._executor()
        mock_resp = _make_response(403, {})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.get        = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.get_positions())
        assert result == []

    def test_network_exception_returns_empty_list(self):
        executor  = self._executor()
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.get        = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = _run(executor.get_positions())
        assert result == []


class TestTradeExecutorHeaders:

    def test_headers_bearer_format(self):
        with patch("execution.trade_executor.settings", _mock_settings()):
            from execution.trade_executor import TradeExecutor
            executor = TradeExecutor()
        headers = executor._headers()
        assert headers["Authorization"] == "Bearer test-api-key"
        assert headers["Accept"] == "application/json"
