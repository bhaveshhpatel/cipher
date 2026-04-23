"""
Regression tests for Tradier stream authentication and 401 handling.

These tests mock httpx responses so no real Tradier API key is needed
in CI. When TRADIER_API_KEY is set in the environment, the integration
test at the bottom also validates the live session-token endpoint.
"""
import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, json_body: dict | None = None):
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body or {})
    resp.raise_for_status = MagicMock(
        side_effect=None if status_code < 400 else Exception(f"HTTP {status_code}")
    )
    return resp


# ---------------------------------------------------------------------------
# Unit: _get_session_token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_token_returns_none_on_401():
    """A 401 from Tradier session endpoint must return None (not raise)."""
    from services.tradier_stream import _get_session_token

    mock_resp = _mock_response(401)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("services.tradier_stream.httpx.AsyncClient", return_value=mock_client):
        result = await _get_session_token()

    assert result is None, "Expected None when Tradier returns 401 on session endpoint"


@pytest.mark.asyncio
async def test_get_session_token_returns_sessionid_on_success():
    """A 200 response with a valid sessionid must be returned correctly."""
    from services.tradier_stream import _get_session_token

    mock_resp = _mock_response(200, {"stream": {"sessionid": "abc-123", "url": "https://stream.tradier.com"}})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("services.tradier_stream.httpx.AsyncClient", return_value=mock_client):
        result = await _get_session_token()

    assert result == "abc-123", "Expected sessionid to be returned on success"


@pytest.mark.asyncio
async def test_get_session_token_returns_none_on_exception():
    """A network-level exception must be caught and return None."""
    from services.tradier_stream import _get_session_token

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

    with patch("services.tradier_stream.httpx.AsyncClient", return_value=mock_client):
        result = await _get_session_token()

    assert result is None


# ---------------------------------------------------------------------------
# Unit: stream_options_flow — 401 on stream endpoint falls back to demo mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_401_falls_back_to_demo_mode():
    """
    When the stream POST returns 401, stream_options_flow must fall back
    to _demo_mode instead of looping forever with bad auth requests.
    """
    from services import tradier_stream

    # Patch _get_session_token to return a valid token
    async def fake_session_token():
        return "valid-session-id"

    demo_called = []

    async def fake_demo_mode(symbols):
        demo_called.append(symbols)
        # Don't actually loop — just return immediately
        return

    # Build a mock streaming context manager that returns 401
    mock_stream_resp = MagicMock()
    mock_stream_resp.status_code = 401
    mock_stream_resp.__aenter__ = AsyncMock(return_value=mock_stream_resp)
    mock_stream_resp.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(tradier_stream, "_get_session_token", fake_session_token),
        patch.object(tradier_stream, "_demo_mode", fake_demo_mode),
        patch("services.tradier_stream.httpx.AsyncClient", return_value=mock_client),
    ):
        await tradier_stream.stream_options_flow(["AAPL", "SPY"])

    assert demo_called, "_demo_mode must be called when stream returns 401"
    assert demo_called[0] == ["AAPL", "SPY"]


# ---------------------------------------------------------------------------
# Integration: live Tradier session-token endpoint (skipped in CI)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("TRADIER_API_KEY"),
    reason="TRADIER_API_KEY not set — skipping live Tradier integration test",
)
@pytest.mark.asyncio
async def test_live_tradier_session_token():
    """
    Validates that the configured TRADIER_API_KEY can successfully obtain
    a streaming session token from Tradier's live endpoint.

    Equivalent curl:
        curl -X POST https://api.tradier.com/v1/markets/events/session \
             -H 'Authorization: Bearer $TRADIER_API_KEY' \
             -H 'Accept: application/json'

    Run locally with:
        TRADIER_API_KEY=<your_key> pytest backend/tests/test_tradier_stream.py \
            -k test_live_tradier_session_token -v
    """
    from services.tradier_stream import _get_session_token

    token = await _get_session_token()

    assert token is not None, (
        "Live Tradier session token was None. "
        "Check TRADIER_API_KEY, TRADIER_BASE_URL, and that the key has "
        'the \'Market Data\' permission enabled in your Tradier account."
    )
    assert isinstance(token, str) and len(token) > 0, "Session token must be a non-empty string"
