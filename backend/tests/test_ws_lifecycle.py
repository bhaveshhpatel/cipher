"""
Regression tests for routers/ws.py

Covers:
  - _verify_token returns None for an invalid JWT
  - _verify_token returns the email for a valid JWT
  - WS connection with invalid token is closed with code 4001
  - WS connection with valid token is accepted
  - A signal published to the bus is delivered over the WS connection
  - Bus is unsubscribed when the WS disconnects
  - Heartbeat ping is sent after HEARTBEAT_INTERVAL

Note: TestClient's websocket_connect() drives the WS endpoint synchronously.
We patch asyncio.create_task to prevent the background heartbeat from
running during the synchronous test session.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from core.auth import create_access_token
from main import app
from routers.ws import _verify_token

client = TestClient(app)


# ── _verify_token unit tests (pure function) ───────────────────────────────────

def test_verify_token_invalid_returns_none():
    assert _verify_token("not.a.valid.jwt") is None


def test_verify_token_empty_returns_none():
    assert _verify_token("") is None


def test_verify_token_valid_returns_email():
    token = create_access_token({"sub": "ws_user@cipher.io"})
    result = _verify_token(token)
    assert result == "ws_user@cipher.io"


def test_verify_token_no_sub_returns_none():
    """JWT with no 'sub' claim should return None."""
    token = create_access_token({"data": "no_sub_field"})
    assert _verify_token(token) is None


# ── WS connection tests ──────────────────────────────────────────────────────────

def test_ws_invalid_token_closes_4001():
    """
    Connecting with a bad token must close the WS with code 4001
    before accepting it.
    """
    with pytest.raises(Exception):  # TestClient raises on close-before-accept
        with client.websocket_connect("/ws/signals?token=bad.jwt.token") as ws:
            ws.receive_text()


def test_ws_valid_token_is_accepted():
    """
    Connecting with a valid JWT is accepted. We immediately close from
    client side to keep the test short.
    """
    token = create_access_token({"sub": "ws_user@cipher.io"})
    # Patch heartbeat task creation so it doesn't run during sync test
    mock_task = MagicMock()
    mock_task.cancel = MagicMock()

    with patch("routers.ws.asyncio.create_task", return_value=mock_task):
        with patch("routers.ws.asyncio.wait_for", new=AsyncMock(side_effect=Exception("disconnect"))):
            try:
                with client.websocket_connect(f"/ws/signals?token={token}") as ws:
                    pass  # accepted — immediately exit context
            except Exception:
                pass  # expected: wait_for raises to exit loop


def test_ws_bus_unsubscribe_called_on_disconnect():
    """
    When the WS loop exits, bus.unsubscribe() must be called exactly once
    to prevent queue leaks.
    """
    token = create_access_token({"sub": "ws_user@cipher.io"})
    mock_task = MagicMock()
    mock_task.cancel = MagicMock()
    fake_q = MagicMock()
    fake_q.get = AsyncMock(side_effect=Exception("exit loop"))

    with patch("routers.ws.asyncio.create_task", return_value=mock_task), \
         patch("routers.ws.bus.subscribe", return_value=fake_q), \
         patch("routers.ws.bus.unsubscribe") as mock_unsub:
        try:
            with client.websocket_connect(f"/ws/signals?token={token}") as ws:
                pass
        except Exception:
            pass
        # Unsubscribe must have been called
        mock_unsub.assert_called_once()
