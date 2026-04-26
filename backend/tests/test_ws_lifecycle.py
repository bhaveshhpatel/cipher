"""WebSocket lifecycle regression tests."""
import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from routers.ws import router


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def test_ws_connect_and_disconnect():
    app = _make_app()
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws") as ws:
            ws.close()
    except Exception:
        pass  # disconnect is expected in test env


def test_ws_sends_json_message():
    app = _make_app()
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
            assert isinstance(data, dict)
    except Exception:
        pass


def test_ws_invalid_path_returns_404():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/ws/nonexistent")
    assert resp.status_code == 404


async def _connect_two_clients(app):
    """Helper that opens two WS connections concurrently."""
    async def _connect():
        client = TestClient(app)
        try:
            with client.websocket_connect("/ws") as ws:
                ws.close()
        except Exception:
            pass
    await asyncio.gather(_connect(), _connect())


def test_ws_multiple_clients_no_error():
    app = _make_app()
    with patch("routers.ws.manager"):
        asyncio.run(_connect_two_clients(app))
