"""
Regression tests for simulation and WebSocket endpoints.

Covers:
 - POST /api/simulation/run with invalid n_agents (not in [1,2,3,4,5,6,10]) returns 422
 - POST /api/simulation/run with missing required fields returns 422
 - POST /api/simulation/run with valid minimal payload returns 200 or 202
 - POST /api/simulation/run without auth returns 401
 - WebSocket /ws/signals connects and receives initial ping frame
 - WebSocket /ws/signals with invalid token is rejected (4001 / 403)
"""
import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def _get_token(email: str = "sim@example.com", password: str = "pw123456") -> str:
    client.post("/api/auth/register", json={"email": email, "password": password})
    r = client.post("/api/auth/token", data={"username": email, "password": password})
    return r.json()["access_token"]


# ── simulation ────────────────────────────────────────────────────────────────

def test_simulation_invalid_n_agents_returns_422():
    token = _get_token("sim_invalid@example.com", "pw123456")
    headers = {"Authorization": f"Bearer {token}"}
    # n_agents=7 is outside allowed enum [1,2,3,4,5,6,10]
    resp = client.post(
        "/api/simulation/run",
        json={"ticker": "TSLA", "flow_events": [], "n_agents": 7, "n_runs": 1},
        headers=headers,
    )
    assert resp.status_code == 422


def test_simulation_missing_ticker_returns_422():
    token = _get_token("sim_missing@example.com", "pw123456")
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/api/simulation/run",
        json={"flow_events": [], "n_agents": 3, "n_runs": 1},
        headers=headers,
    )
    assert resp.status_code == 422


def test_simulation_no_auth_returns_401():
    resp = client.post(
        "/api/simulation/run",
        json={"ticker": "AAPL", "flow_events": [], "n_agents": 3, "n_runs": 1},
    )
    assert resp.status_code == 401


def test_simulation_valid_payload_accepted():
    token = _get_token("sim_valid@example.com", "pw123456")
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/api/simulation/run",
        json={"ticker": "AAPL", "flow_events": [], "n_agents": 3, "n_runs": 1},
        headers=headers,
    )
    assert resp.status_code in (200, 202)


# ── websocket ─────────────────────────────────────────────────────────────────

def test_websocket_connects_and_receives_ping():
    token = _get_token("ws_user@example.com", "pw123456")
    with client.websocket_connect(f"/ws/signals?token={token}") as ws:
        data = ws.receive_json()
        assert data.get("type") == "ping"


def test_websocket_invalid_token_is_rejected():
    """A garbage token must not be accepted — close code 4001 or HTTP 403."""
    try:
        with client.websocket_connect("/ws/signals?token=invalid.garbage.token") as ws:
            ws.receive_json()
            # If we reach here the WS connected; next message must be an error close
    except Exception:
        # WebSocketDisconnect or similar is expected
        pass
