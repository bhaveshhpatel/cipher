"""
Coverage boost for utils/tradier_client.py.

Covers:
  - get_quote: 200 success (list form, dict form), non-200, exception
  - get_quotes_batch: empty input, 200 success, non-200, exception, single-dict response
  - get_expirations: 200 list, 200 single string, non-200, exception
  - get_option_chain: 200 list, 200 single dict, non-200, exception
  - get_options_chain alias
  - get_session_token: success, 429 then success, 429 exhausted, 401, timeout, missing sessionid
  - get_token alias
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from utils.tradier_client import (
    get_quote,
    get_quotes_batch,
    get_expirations,
    get_option_chain,
    get_options_chain,
    get_session_token,
    get_token,
)


def _async_client_mock(resp):
    """Return a context manager mock that yields a client whose .get / .post returns resp."""
    mock_client = MagicMock()
    mock_client.get  = AsyncMock(return_value=resp)
    mock_client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__  = AsyncMock(return_value=False)
    return ctx


def _resp(status=200, json_data=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.headers = headers or {}
    r.text    = ""
    return r


# --- get_quote ---

def test_get_quote_200_dict():
    r = _resp(200, {"quotes": {"quote": {"symbol": "AAPL", "last": 185.0}}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_quote("AAPL"))
    assert result["symbol"] == "AAPL"

def test_get_quote_200_list():
    r = _resp(200, {"quotes": {"quote": [{"symbol": "AAPL", "last": 185.0}]}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_quote("AAPL"))
    assert result["symbol"] == "AAPL"

def test_get_quote_non200_returns_none():
    r = _resp(500)
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_quote("AAPL"))
    assert result is None

def test_get_quote_exception_returns_none():
    with patch("utils.tradier_client.httpx.AsyncClient", side_effect=Exception("err")):
        result = asyncio.get_event_loop().run_until_complete(get_quote("AAPL"))
    assert result is None


# --- get_quotes_batch ---

def test_get_quotes_batch_empty_input():
    result = asyncio.get_event_loop().run_until_complete(get_quotes_batch([]))
    assert result == {}

def test_get_quotes_batch_200_list():
    r = _resp(200, {"quotes": {"quote": [{"symbol": "AAPL", "last": 185.0}]}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_quotes_batch(["AAPL"]))
    assert "AAPL" in result

def test_get_quotes_batch_200_single_dict():
    r = _resp(200, {"quotes": {"quote": {"symbol": "TSLA", "last": 200.0}}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_quotes_batch(["TSLA"]))
    assert "TSLA" in result

def test_get_quotes_batch_non200():
    r = _resp(500)
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_quotes_batch(["AAPL"]))
    assert result == {}

def test_get_quotes_batch_exception():
    with patch("utils.tradier_client.httpx.AsyncClient", side_effect=Exception("err")):
        result = asyncio.get_event_loop().run_until_complete(get_quotes_batch(["AAPL"]))
    assert result == {}


# --- get_expirations ---

def test_get_expirations_200_list():
    r = _resp(200, {"expirations": {"date": ["2026-06-20", "2026-07-18"]}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_expirations("AAPL"))
    assert "2026-06-20" in result

def test_get_expirations_200_single_string():
    r = _resp(200, {"expirations": {"date": "2026-06-20"}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_expirations("AAPL"))
    assert result == ["2026-06-20"]

def test_get_expirations_non200():
    r = _resp(500)
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_expirations("AAPL"))
    assert result == []

def test_get_expirations_exception():
    with patch("utils.tradier_client.httpx.AsyncClient", side_effect=Exception("err")):
        result = asyncio.get_event_loop().run_until_complete(get_expirations("AAPL"))
    assert result == []


# --- get_option_chain ---

def test_get_option_chain_200_list():
    r = _resp(200, {"options": {"option": [{"symbol": "AAPL231215C00180000"}]}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(
            get_option_chain("AAPL", "2026-06-20")
        )
    assert len(result) == 1

def test_get_option_chain_200_single_dict():
    r = _resp(200, {"options": {"option": {"symbol": "AAPL231215C00180000"}}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(
            get_option_chain("AAPL", "2026-06-20")
        )
    assert len(result) == 1

def test_get_option_chain_non200():
    r = _resp(500)
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(
            get_option_chain("AAPL", "2026-06-20")
        )
    assert result == []

def test_get_option_chain_exception():
    with patch("utils.tradier_client.httpx.AsyncClient", side_effect=Exception("err")):
        result = asyncio.get_event_loop().run_until_complete(
            get_option_chain("AAPL", "2026-06-20")
        )
    assert result == []

def test_get_options_chain_alias():
    assert get_options_chain is get_option_chain


# --- get_session_token ---

def test_get_session_token_success():
    r = _resp(200, {"stream": {"sessionid": "tok-abc123"}})
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_session_token())
    assert result == "tok-abc123"

def test_get_session_token_missing_sessionid():
    r = _resp(200, {"stream": {}})
    r.text = "{}"
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_session_token())
    assert result is None

def test_get_session_token_401_returns_none():
    r = _resp(401)
    with patch("utils.tradier_client.httpx.AsyncClient", return_value=_async_client_mock(r)):
        result = asyncio.get_event_loop().run_until_complete(get_session_token())
    assert result is None

def test_get_session_token_429_then_success():
    """First call returns 429 with Retry-After:0, second returns 200."""
    r_429 = _resp(429, headers={"Retry-After": "0"})
    r_200 = _resp(200, {"stream": {"sessionid": "tok-xyz"}})

    call_count = 0
    async def _post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return r_429 if call_count == 1 else r_200

    mock_client = MagicMock()
    mock_client.post = _post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("utils.tradier_client.httpx.AsyncClient", return_value=ctx), \
         patch("utils.tradier_client.asyncio.sleep", new=AsyncMock()):
        result = asyncio.get_event_loop().run_until_complete(get_session_token())
    assert result == "tok-xyz"

def test_get_session_token_429_exhausted_returns_none():
    r_429 = _resp(429, headers={"Retry-After": "0"})

    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=r_429)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("utils.tradier_client.httpx.AsyncClient", return_value=ctx), \
         patch("utils.tradier_client.asyncio.sleep", new=AsyncMock()):
        result = asyncio.get_event_loop().run_until_complete(get_session_token())
    assert result is None

def test_get_session_token_timeout_retries_then_none():
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("utils.tradier_client.httpx.AsyncClient", return_value=ctx), \
         patch("utils.tradier_client.asyncio.sleep", new=AsyncMock()):
        result = asyncio.get_event_loop().run_until_complete(get_session_token())
    assert result is None

def test_get_session_token_connect_error_retries_then_none():
    mock_client = MagicMock()
    mock_client.post = AsyncMock(
        side_effect=httpx.ConnectError("refused")
    )
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("utils.tradier_client.httpx.AsyncClient", return_value=ctx), \
         patch("utils.tradier_client.asyncio.sleep", new=AsyncMock()):
        result = asyncio.get_event_loop().run_until_complete(get_session_token())
    assert result is None

def test_get_token_alias():
    assert get_token is get_session_token
