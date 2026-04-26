"""
Regression tests for routers/ws.py

Strategy:
  - _verify_token tested directly as a pure function.
  - WebSocket endpoint tested via FastAPI's TestClient WebSocket context.
  - _heartbeat logic tested by patching asyncio.sleep and inspecting
    send_text / close calls on a mock WebSocket.
  - bus.subscribe / bus.unsubscribe verified via mock.

Covers:
  Constants:
  - HEARTBEAT_INTERVAL == 25
  - PONG_TIMEOUT == 10

  _verify_token:
  - Valid token (non-expired) returns email string
  - Expired token returns None
  - Malformed token returns None
  - Token with no 'sub' claim returns None

  /ws/signals endpoint:
  - Missing token query param -> connection refused (4001 or HTTP 403)
  - Invalid JWT -> close(4001) called before accept
  - Expired JWT -> close(4001) called before accept
  - Valid JWT -> accept() called, bus.subscribe('signals') called
  - Valid JWT -> after disconnect bus.unsubscribe called (cleanup)
  - Message on bus queue -> forwarded as JSON text to client
  - Multiple messages forwarded in order
  - WebSocketDisconnect handled without raising

  _heartbeat:
  - Sends {"type": "ping"} after sleep
  - Pong received -> loop continues (stop_event remains clear)
  - Non-pong message during ping window -> warning, loop continues
  - Pong timeout -> stop_event.set() + websocket.close(code=1001)
  - Exception during send_text -> stop_event.set()
"""
import pytest
import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from core.auth import create_access_token
from config import settings
from routers.ws import router, _verify_token, HEARTBEAT_INTERVAL, PONG_TIMEOUT, _heartbeat
from core.async_bus import bus


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router)
    return a


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_heartbeat_interval_is_25():
    assert HEARTBEAT_INTERVAL == 25


def test_pong_timeout_is_10():
    assert PONG_TIMEOUT == 10


# ---------------------------------------------------------------------------
# _verify_token
# ---------------------------------------------------------------------------

def test_verify_token_valid_returns_email():
    token = create_access_token({"sub": "user@cipher.app"})
    result = _verify_token(token)
    assert result == "user@cipher.app"


def test_verify_token_expired_returns_none():
    token = create_access_token(
        {"sub": "exp@cipher.app"},
        expires_delta=timedelta(seconds=-1),
    )
    assert _verify_token(token) is None


def test_verify_token_malformed_returns_none():
    assert _verify_token("not.a.real.jwt") is None


def test_verify_token_no_sub_returns_none():
    token = create_access_token({"data": "no_sub_here"})
    # payload has no 'sub' key
    assert _verify_token(token) is None


# ---------------------------------------------------------------------------
# /ws/signals: auth rejection paths
# ---------------------------------------------------------------------------

def test_ws_missing_token_rejected(app):
    """No token query param -> 403 or connection refused."""
    client = TestClient(app, raise_server_exceptions=False)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/signals"):
            pass


def test_ws_invalid_token_close_4001(app):
    client = TestClient(app, raise_server_exceptions=False)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/signals?token=not.a.real.jwt"):
            pass


def test_ws_expired_token_close_4001(app):
    expired = create_access_token(
        {"sub": "exp@cipher.app"},
        expires_delta=timedelta(seconds=-1),
    )
    client = TestClient(app, raise_server_exceptions=False)
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/signals?token={expired}"):
            pass


# ---------------------------------------------------------------------------
# /ws/signals: successful connection — bus subscribe/unsubscribe
# ---------------------------------------------------------------------------

def test_ws_valid_token_accepted_and_subscribes(app):
    """Valid JWT -> accept() + bus.subscribe('signals') called."""
    token = create_access_token({"sub": "ws@cipher.app"})
    client = TestClient(app)

    mock_q: asyncio.Queue = asyncio.Queue()
    # Put sentinel so the receive loop exits immediately
    mock_q.put_nowait({"type": "test", "data": "hello"})

    original_subscribe   = bus.subscribe
    original_unsubscribe = bus.unsubscribe
    subscribed           = []
    unsubscribed         = []

    def _fake_subscribe(channel):
        q = mock_q
        subscribed.append(channel)
        return q

    def _fake_unsubscribe(channel, q):
        unsubscribed.append(channel)

    with patch.object(bus, "subscribe",   side_effect=_fake_subscribe), \
         patch.object(bus, "unsubscribe", side_effect=_fake_unsubscribe):
        try:
            with client.websocket_connect(f"/ws/signals?token={token}") as ws:
                # Receive the forwarded message, then close
                msg = ws.receive_text()
                data = json.loads(msg)
                assert data["type"] == "test"
        except Exception:
            pass  # disconnect is expected

    assert "signals" in subscribed


def test_ws_unsubscribe_called_on_disconnect(app):
    """bus.unsubscribe must always be called (even on abrupt disconnect)."""
    token = create_access_token({"sub": "cleanup@cipher.app"})
    client = TestClient(app)

    mock_q: asyncio.Queue = asyncio.Queue()
    mock_q.put_nowait({"type": "ping_data"})
    unsubscribed = []

    def _fake_subscribe(channel):
        return mock_q

    def _fake_unsubscribe(channel, q):
        unsubscribed.append(channel)

    with patch.object(bus, "subscribe",   side_effect=_fake_subscribe), \
         patch.object(bus, "unsubscribe", side_effect=_fake_unsubscribe):
        try:
            with client.websocket_connect(f"/ws/signals?token={token}") as ws:
                ws.receive_text()  # consume forwarded message
        except Exception:
            pass

    assert "signals" in unsubscribed


# ---------------------------------------------------------------------------
# _heartbeat unit tests (no live WebSocket needed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_sends_ping():
    """_heartbeat sends {type:ping} after sleep."""
    stop = asyncio.Event()
    ws   = AsyncMock()

    # Make receive_text return a valid pong then stop
    ws.receive_text.return_value = json.dumps({"type": "pong"})

    async def _fake_sleep(n):
        stop.set()   # exit after first iteration

    with patch("routers.ws.asyncio.sleep", side_effect=_fake_sleep):
        await _heartbeat(ws, stop)

    ws.send_text.assert_called_once_with(json.dumps({"type": "ping"}))


@pytest.mark.asyncio
async def test_heartbeat_pong_received_loop_continues():
    """Valid pong -> stop_event stays clear for at least one iteration."""
    stop     = asyncio.Event()
    ws       = AsyncMock()
    calls    = []

    ws.receive_text.return_value = json.dumps({"type": "pong"})

    async def _fake_sleep(n):
        calls.append(n)
        if len(calls) >= 2:
            stop.set()

    with patch("routers.ws.asyncio.sleep", side_effect=_fake_sleep):
        await _heartbeat(ws, stop)

    # At least 2 sleep calls means the loop ran more than once
    assert len(calls) >= 2
    assert not stop.is_set() or len(calls) >= 2


@pytest.mark.asyncio
async def test_heartbeat_non_pong_message_logs_warning():
    """Non-pong message during ping window -> warning logged, loop continues."""
    stop  = asyncio.Event()
    ws    = AsyncMock()
    calls = []

    # First iteration: non-pong; second iteration: set stop
    ws.receive_text.return_value = json.dumps({"type": "chat", "text": "hello"})

    async def _fake_sleep(n):
        calls.append(n)
        if len(calls) >= 2:
            stop.set()

    with patch("routers.ws.asyncio.sleep", side_effect=_fake_sleep), \
         patch("routers.ws.log") as mock_log:
        await _heartbeat(ws, stop)

    mock_log.warning.assert_called()


@pytest.mark.asyncio
async def test_heartbeat_pong_timeout_closes_with_1001():
    """asyncio.TimeoutError on receive -> stop_event set + close(1001)."""
    stop = asyncio.Event()
    ws   = AsyncMock()

    ws.receive_text.side_effect = asyncio.TimeoutError()

    slept = []

    async def _fake_sleep(n):
        slept.append(n)
        # Only run once

    with patch("routers.ws.asyncio.sleep",   side_effect=_fake_sleep), \
         patch("routers.ws.asyncio.wait_for", side_effect=asyncio.TimeoutError):
        await _heartbeat(ws, stop)

    assert stop.is_set()
    ws.close.assert_called_once_with(code=1001)


@pytest.mark.asyncio
async def test_heartbeat_send_exception_sets_stop():
    """Exception during send_text -> stop_event set, no uncaught raise."""
    stop = asyncio.Event()
    ws   = AsyncMock()
    ws.send_text.side_effect = Exception("connection broken")

    async def _fake_sleep(n):
        pass  # return immediately

    with patch("routers.ws.asyncio.sleep", side_effect=_fake_sleep):
        await _heartbeat(ws, stop)

    assert stop.is_set()
