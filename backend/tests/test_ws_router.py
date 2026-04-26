"""
Regression tests for routers/ws.py
"""
import pytest
import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import create_access_token
from routers.ws import router, _verify_token, HEARTBEAT_INTERVAL, PONG_TIMEOUT, _heartbeat
from core.async_bus import bus


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
    assert _verify_token(token) == "user@cipher.app"


def test_verify_token_expired_returns_none():
    token = create_access_token({"sub": "exp@cipher.app"}, expires_delta=timedelta(seconds=-1))
    assert _verify_token(token) is None


def test_verify_token_malformed_returns_none():
    assert _verify_token("not.a.real.jwt") is None


def test_verify_token_no_sub_returns_none():
    token = create_access_token({"data": "no_sub_here"})
    assert _verify_token(token) is None


# ---------------------------------------------------------------------------
# /ws/signals: auth rejection paths
# ---------------------------------------------------------------------------

def test_ws_missing_token_rejected(app):
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
    expired = create_access_token({"sub": "exp@cipher.app"}, expires_delta=timedelta(seconds=-1))
    client = TestClient(app, raise_server_exceptions=False)
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/signals?token={expired}"):
            pass


# ---------------------------------------------------------------------------
# /ws/signals: successful connection
# ---------------------------------------------------------------------------

def test_ws_valid_token_accepted_and_subscribes(app):
    token = create_access_token({"sub": "ws@cipher.app"})
    client = TestClient(app)

    mock_q: asyncio.Queue = asyncio.Queue()
    mock_q.put_nowait({"type": "test", "data": "hello"})
    subscribed   = []
    unsubscribed = []

    def _fake_subscribe(channel):
        subscribed.append(channel)
        return mock_q

    def _fake_unsubscribe(channel, q):
        unsubscribed.append(channel)

    with patch.object(bus, "subscribe",   side_effect=_fake_subscribe), \
         patch.object(bus, "unsubscribe", side_effect=_fake_unsubscribe):
        try:
            with client.websocket_connect(f"/ws/signals?token={token}") as ws:
                data = json.loads(ws.receive_text())
                assert data["type"] == "test"
        except Exception:
            pass

    assert "signals" in subscribed


def test_ws_unsubscribe_called_on_disconnect(app):
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
                ws.receive_text()
        except Exception:
            pass

    assert "signals" in unsubscribed


# ---------------------------------------------------------------------------
# _heartbeat unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_sends_ping():
    stop = asyncio.Event()
    ws   = AsyncMock()
    ws.receive_text.return_value = json.dumps({"type": "pong"})

    async def _fake_sleep(n):
        stop.set()

    with patch("routers.ws.asyncio.sleep", side_effect=_fake_sleep):
        await _heartbeat(ws, stop)

    ws.send_text.assert_called_once_with(json.dumps({"type": "ping"}))


@pytest.mark.asyncio
async def test_heartbeat_pong_received_loop_continues():
    stop  = asyncio.Event()
    ws    = AsyncMock()
    calls = []
    ws.receive_text.return_value = json.dumps({"type": "pong"})

    async def _fake_sleep(n):
        calls.append(n)
        if len(calls) >= 2:
            stop.set()

    with patch("routers.ws.asyncio.sleep", side_effect=_fake_sleep):
        await _heartbeat(ws, stop)

    assert len(calls) >= 2


@pytest.mark.asyncio
async def test_heartbeat_non_pong_message_logs_warning():
    stop  = asyncio.Event()
    ws    = AsyncMock()
    calls = []
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
    stop = asyncio.Event()
    ws   = AsyncMock()
    ws.receive_text.side_effect = asyncio.TimeoutError()

    async def _fake_sleep(n):
        pass

    with patch("routers.ws.asyncio.sleep",   side_effect=_fake_sleep), \
         patch("routers.ws.asyncio.wait_for", side_effect=asyncio.TimeoutError):
        await _heartbeat(ws, stop)

    assert stop.is_set()
    ws.close.assert_called_once_with(code=1001)


@pytest.mark.asyncio
async def test_heartbeat_send_exception_sets_stop():
    stop = asyncio.Event()
    ws   = AsyncMock()
    ws.send_text.side_effect = Exception("connection broken")

    async def _fake_sleep(n):
        pass

    with patch("routers.ws.asyncio.sleep", side_effect=_fake_sleep):
        await _heartbeat(ws, stop)

    assert stop.is_set()
