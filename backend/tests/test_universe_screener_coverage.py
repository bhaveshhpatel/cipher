"""
Coverage boost for services/universe_screener.py.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from services.universe_screener import (
    ScreenResult,
    screen_universe,
    get_stream_eligible,
    _nearest_expiry_param,
    _is_stream_eligible,
)


# --- ScreenResult ---

def test_screen_result_total():
    r = ScreenResult(eligible=["AAPL", "TSLA"], ineligible=["XYZ"])
    assert r.total == 3

def test_screen_result_summary_keys():
    r = ScreenResult(eligible=["AAPL"], ineligible=[], priority=["AAPL"],
                     screened=5, duration_s=0.5)
    s = r.summary()
    assert s["eligible"] == 1
    assert s["screened"] == 5
    assert s["source"]   == "screener"


# --- _nearest_expiry_param ---

def test_nearest_expiry_param_is_future_friday():
    from datetime import date
    s = _nearest_expiry_param()
    d = date.fromisoformat(s)
    assert d >= date.today()
    assert d.weekday() == 4


# --- screen_universe: empty ---

def test_screen_universe_empty_returns_empty():
    result = asyncio.run(screen_universe([]))
    assert result.eligible == []
    assert result.ineligible == []


# --- screen_universe: no API key, default=True ---

def test_screen_universe_no_key_default_true():
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY                  = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = True
        ms.priority_symbols                 = []
        ms.UNIVERSE_BATCH_DELAY_MS          = 0
        result = asyncio.run(screen_universe(["AAPL", "TSLA"]))
    assert "AAPL" in result.eligible
    assert "TSLA" in result.eligible


def test_screen_universe_no_key_default_false():
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY                  = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = False
        ms.priority_symbols                 = []
        ms.UNIVERSE_BATCH_DELAY_MS          = 0
        result = asyncio.run(screen_universe(["AAPL", "TSLA"]))
    assert "AAPL" in result.ineligible
    assert "TSLA" in result.ineligible


# --- screen_universe: priority symbols ---

def test_screen_universe_priority_symbols_always_eligible():
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY                  = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = False
        ms.priority_symbols                 = ["AAPL"]
        ms.UNIVERSE_BATCH_DELAY_MS          = 0
        result = asyncio.run(screen_universe(["AAPL", "TSLA"]))
    assert "AAPL" in result.eligible
    assert "AAPL" in result.priority


# --- _is_stream_eligible: HTTP mocks ---

def _make_http_ctx(status, json_data=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__  = AsyncMock(return_value=False)
    return ctx


def test_is_stream_eligible_with_oi():
    ctx = _make_http_ctx(200, {"options": {"option": [{"open_interest": 500}]}})
    with patch("services.universe_screener.settings") as ms, \
         patch("services.universe_screener.httpx.AsyncClient", return_value=ctx):
        ms.TRADIER_API_KEY                  = "fake"
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = False
        result = asyncio.run(_is_stream_eligible("AAPL"))
    assert result == "AAPL"


def test_is_stream_eligible_no_oi_default_false():
    ctx = _make_http_ctx(200, {"options": {"option": [{"open_interest": 0}]}})
    with patch("services.universe_screener.settings") as ms, \
         patch("services.universe_screener.httpx.AsyncClient", return_value=ctx):
        ms.TRADIER_API_KEY                  = "fake"
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = False
        result = asyncio.run(_is_stream_eligible("XYZ"))
    assert result is None


def test_is_stream_eligible_no_oi_default_true():
    ctx = _make_http_ctx(200, {"options": {"option": [{"open_interest": 0}]}})
    with patch("services.universe_screener.settings") as ms, \
         patch("services.universe_screener.httpx.AsyncClient", return_value=ctx):
        ms.TRADIER_API_KEY                  = "fake"
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = True
        result = asyncio.run(_is_stream_eligible("XYZ"))
    assert result == "XYZ"


def test_is_stream_eligible_non200_default_false():
    ctx = _make_http_ctx(500)
    with patch("services.universe_screener.settings") as ms, \
         patch("services.universe_screener.httpx.AsyncClient", return_value=ctx):
        ms.TRADIER_API_KEY                  = "fake"
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = False
        result = asyncio.run(_is_stream_eligible("AAPL"))
    assert result is None


def test_is_stream_eligible_exception_default_true():
    with patch("services.universe_screener.settings") as ms, \
         patch("services.universe_screener.httpx.AsyncClient",
               side_effect=RuntimeError("conn err")):
        ms.TRADIER_API_KEY                  = "fake"
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = True
        result = asyncio.run(_is_stream_eligible("AAPL"))
    assert result == "AAPL"


def test_screen_universe_with_api_key_eligible():
    ctx = _make_http_ctx(200, {"options": {"option": [{"open_interest": 500}]}})
    with patch("services.universe_screener.settings") as ms, \
         patch("services.universe_screener.httpx.AsyncClient", return_value=ctx):
        ms.TRADIER_API_KEY                  = "fake-key"
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = False
        ms.priority_symbols                 = []
        ms.UNIVERSE_BATCH_DELAY_MS          = 0
        result = asyncio.run(screen_universe(["AAPL"]))
    assert "AAPL" in result.eligible


def test_get_stream_eligible_returns_list():
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY                  = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = True
        ms.priority_symbols                 = []
        ms.UNIVERSE_BATCH_DELAY_MS          = 0
        result = asyncio.run(get_stream_eligible(["AAPL", "TSLA"]))
    assert isinstance(result, list)
    assert "AAPL" in result
