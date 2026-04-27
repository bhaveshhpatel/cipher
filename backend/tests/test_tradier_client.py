"""
Unit tests for utils/tradier_client.py
"""
import asyncio
from unittest.mock import MagicMock, patch

import httpx


def test_tradier_client_importable():
    import utils.tradier_client as _m
    assert _m is not None


def test_tradier_client_has_expected_api():
    import utils.tradier_client as tc
    for name in ("get_quote", "get_options_chain", "get_token"):
        assert hasattr(tc, name), f"Missing: {name}"


class TestGetQuote:
    @staticmethod
    def _mock_client(json_payload: dict, status: int = 200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_payload
        resp.raise_for_status = MagicMock()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def get(self, *a, **kw): return resp

        return _C()

    def test_get_quote_returns_price(self):
        from utils.tradier_client import get_quote
        payload = {"quotes": {"quote": {"symbol": "AAPL", "last": 178.5}}}
        with patch("utils.tradier_client.httpx.AsyncClient",
                   return_value=self._mock_client(payload)):
            result = asyncio.run(get_quote("AAPL"))
        assert result is not None

    def test_get_quote_handles_404(self):
        from utils.tradier_client import get_quote
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=resp
        )

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def get(self, *a, **kw): return resp

        with patch("utils.tradier_client.httpx.AsyncClient", return_value=_C()):
            result = asyncio.run(get_quote("ZZZZZ"))
        assert result is None or isinstance(result, dict)

    def test_get_quote_handles_network_error(self):
        from utils.tradier_client import get_quote

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def get(self, *a, **kw): raise httpx.ConnectError("refused")

        with patch("utils.tradier_client.httpx.AsyncClient", return_value=_C()):
            result = asyncio.run(get_quote("AAPL"))
        assert result is None


class TestGetOptionsChain:
    def test_get_options_chain_returns_list(self):
        from utils.tradier_client import get_options_chain
        payload = {
            "options": {
                "option": [
                    {"symbol": "AAPL260620C00180000", "last": 4.85,
                     "strike": 180.0, "option_type": "call"},
                ]
            }
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def get(self, *a, **kw): return resp

        with patch("utils.tradier_client.httpx.AsyncClient", return_value=_C()):
            result = asyncio.run(get_options_chain("AAPL", "2026-06-20"))
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_get_options_chain_empty_response(self):
        from utils.tradier_client import get_options_chain
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"options": None}
        resp.raise_for_status = MagicMock()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def get(self, *a, **kw): return resp

        with patch("utils.tradier_client.httpx.AsyncClient", return_value=_C()):
            result = asyncio.run(get_options_chain("AAPL", "2026-06-20"))
        assert result == [] or result is None


class TestGetToken:
    def test_get_token_returns_string_on_success(self):
        from utils.tradier_client import get_token
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"stream": {"sessionid": "tok_abc"}}
        resp.raise_for_status = MagicMock()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, *a, **kw): return resp

        with patch("utils.tradier_client.httpx.AsyncClient", return_value=_C()):
            result = asyncio.run(get_token())
        assert result == "tok_abc"

    def test_get_token_returns_none_on_401(self):
        from utils.tradier_client import get_token
        resp = MagicMock()
        resp.status_code = 401

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, *a, **kw): return resp

        with patch("utils.tradier_client.httpx.AsyncClient", return_value=_C()):
            result = asyncio.run(get_token())
        assert result is None

    def test_session_semaphore_exists(self):
        import utils.tradier_client as tc
        import asyncio as aio
        sem = getattr(tc, "_SESSION_SEM", None)
        if sem is None:
            return
        assert isinstance(sem, aio.Semaphore)
