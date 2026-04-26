"""
Regression tests for the /health/stream endpoint (B-008 stream health).

Covers:
 - GET /health/stream returns HTTP 200
 - Response body contains 'status' key (via 'mode' field in StreamHealthOut)
 - mode value is a non-empty string
 - Endpoint is reachable without extra headers (auth mocked)
 - Multiple consecutive calls all return 200 (idempotency)
 - Degraded mode still returns 200
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from routers.health import router

# Stub out auth so tests are not blocked by JWT validation
_AUTH_OVERRIDE = {"sub": "test@example.com", "email": "test@example.com"}


@pytest.fixture
def client():
    from core.auth import get_current_user
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _AUTH_OVERRIDE
    return TestClient(app)


_HEALTHY_STATS = {
    "mode": "live",
    "active_symbols": 42,
    "ticks": 1000,
    "classified": 980,
    "deduped": 20,
    "signals": 50,
    "errors": 0,
    "reconnects": 1,
    "last_tick_at": 1700000000.0,
    "last_reconnect_at": None,
    "uptime_seconds": 3600.0,
}


def test_health_returns_200(client):
    with patch("routers.health.get_stats", return_value=_HEALTHY_STATS):
        resp = client.get("/health/stream")
    assert resp.status_code == 200


def test_health_response_has_mode_key(client):
    with patch("routers.health.get_stats", return_value=_HEALTHY_STATS):
        resp = client.get("/health/stream")
    body = resp.json()
    assert "mode" in body


def test_health_mode_is_nonempty_string(client):
    with patch("routers.health.get_stats", return_value=_HEALTHY_STATS):
        resp = client.get("/health/stream")
    body = resp.json()
    assert isinstance(body["mode"], str)
    assert len(body["mode"]) > 0


def test_health_requires_no_extra_headers(client):
    """Health check must be reachable without any extra headers (auth is overridden)."""
    with patch("routers.health.get_stats", return_value=_HEALTHY_STATS):
        resp = client.get("/health/stream")
    assert resp.status_code == 200


def test_health_is_idempotent(client):
    """Calling health three times in a row must all return 200."""
    with patch("routers.health.get_stats", return_value=_HEALTHY_STATS):
        for _ in range(3):
            resp = client.get("/health/stream")
            assert resp.status_code == 200


def test_health_degraded_mode_still_returns_200(client):
    """A degraded (but running) service must still return 200, not 5xx."""
    degraded = {**_HEALTHY_STATS, "mode": "reconnecting", "errors": 5}
    with patch("routers.health.get_stats", return_value=degraded):
        resp = client.get("/health/stream")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "reconnecting"
