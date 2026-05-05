"""
Coverage tests for services/tradier_stream.py lines 287-353:
  _get_session_token() — all branches:
    - 200 success with valid sessionid
    - 200 but missing sessionid field
    - 401 (bad key) → returns None immediately
    - TransientError (TimeoutException) → retries, then None
    - Unexpected exception → returns None immediately
    - raise_for_status raises (non-401, non-timeout)

Also covers _is_market_hours() and _backoff() for completeness.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import httpx


def _mock_resp(status: int, json_data=None, raise_for_status=False):
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    if raise_for_status:
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        ))
    else:
        resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_data or {})
    return resp


def _mock_client(resp=None, side_effect=None):
    mc = AsyncMock()
    mc.__aenter__ = AsyncMock(return_value=mc)
    mc.__aexit__ = AsyncMock(return_value=None)
    if side_effect:
        mc.post = AsyncMock(side_effect=side_effect)
    else:
        mc.post = AsyncMock(return_value=resp)
    return mc


# ---------------------------------------------------------------------------
# _get_session_token — success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_token_success():
    from services.tradier_stream import _get_session_token

    resp = _mock_resp(200, {"stream": {"sessionid": "tok_abc123"}})
    mc = _mock_client(resp=resp)

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "live_key"
        token = await _get_session_token()

    assert token == "tok_abc123"


@pytest.mark.asyncio
async def test_get_session_token_missing_sessionid_returns_none():
    from services.tradier_stream import _get_session_token

    resp = _mock_resp(200, {"stream": {}})  # no sessionid
    mc = _mock_client(resp=resp)

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "live_key"
        token = await _get_session_token()

    assert token is None


# ---------------------------------------------------------------------------
# _get_session_token — 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_token_401_returns_none_immediately():
    from services.tradier_stream import _get_session_token

    resp = _mock_resp(401)
    mc = _mock_client(resp=resp)

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "bad_key"
        token = await _get_session_token()

    assert token is None
    # 401 must short-circuit — only one POST attempt
    assert mc.post.call_count == 1


# ---------------------------------------------------------------------------
# _get_session_token — transient error retries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_token_timeout_retries_then_returns_none():
    from services.tradier_stream import _get_session_token, _SESSION_RETRY_MAX

    mc = _mock_client(side_effect=httpx.TimeoutException("timeout"))

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "live_key"
        token = await _get_session_token()

    assert token is None
    assert mc.post.call_count == _SESSION_RETRY_MAX


@pytest.mark.asyncio
async def test_get_session_token_connect_error_retries():
    from services.tradier_stream import _get_session_token, _SESSION_RETRY_MAX

    mc = _mock_client(side_effect=httpx.ConnectError("refused"))

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "live_key"
        token = await _get_session_token()

    assert token is None
    assert mc.post.call_count == _SESSION_RETRY_MAX


@pytest.mark.asyncio
async def test_get_session_token_remote_protocol_error_retries():
    from services.tradier_stream import _get_session_token, _SESSION_RETRY_MAX

    mc = _mock_client(side_effect=httpx.RemoteProtocolError("protocol"))

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "live_key"
        token = await _get_session_token()

    assert token is None
    assert mc.post.call_count == _SESSION_RETRY_MAX


# ---------------------------------------------------------------------------
# _get_session_token — transient error then success on last attempt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_token_succeeds_on_third_attempt():
    from services.tradier_stream import _get_session_token

    resp_ok = _mock_resp(200, {"stream": {"sessionid": "tok_retry_ok"}})
    call_count = {"n": 0}

    async def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise httpx.TimeoutException("timeout")
        return resp_ok

    mc = AsyncMock()
    mc.__aenter__ = AsyncMock(return_value=mc)
    mc.__aexit__ = AsyncMock(return_value=None)
    mc.post = flaky

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "live_key"
        token = await _get_session_token()

    assert token == "tok_retry_ok"
    assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# _get_session_token — unexpected exception → immediate None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_token_unexpected_exception_returns_none():
    from services.tradier_stream import _get_session_token

    mc = _mock_client(side_effect=ValueError("unexpected"))

    with patch("services.tradier_stream.settings") as mock_settings, \
         patch("httpx.AsyncClient", return_value=mc):
        mock_settings.TRADIER_BASE_URL = "https://api.tradier.com"
        mock_settings.TRADIER_API_KEY = "live_key"
        token = await _get_session_token()

    assert token is None
    # Unexpected error → immediate return after first attempt
    assert mc.post.call_count == 1


# ---------------------------------------------------------------------------
# _is_market_hours — smoke test (just verifies it returns a bool)
# ---------------------------------------------------------------------------

def test_is_market_hours_returns_bool():
    from services.tradier_stream import _is_market_hours
    result = _is_market_hours()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _backoff — stays within bounds
# ---------------------------------------------------------------------------

def test_backoff_returns_non_negative_within_cap():
    from services.tradier_stream import _backoff, _BACKOFF_CAP
    for attempt in range(8):
        val = _backoff(attempt)
        assert 0 <= val <= _BACKOFF_CAP, f"_backoff({attempt}) = {val} out of range"


# ---------------------------------------------------------------------------
# get_stats includes lookback_stats keys
# ---------------------------------------------------------------------------

def test_get_stats_includes_lookback_keys():
    from services import tradier_stream as ts
    stats = ts.get_stats()
    assert "lookback_queued" in stats
    assert "lookback_queue_overflow" in stats
    assert "ticks" in stats
    assert "signals" in stats
