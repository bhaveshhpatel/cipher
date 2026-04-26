"""
Regression tests for the /api/health endpoint.

Covers:
 - GET /api/health returns HTTP 200
 - Response body contains 'status' key
 - Status value is a non-empty string
 - Endpoint is reachable without authentication
 - Multiple consecutive calls all return 200 (idempotency)
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from routers.health import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_health_returns_200(client):
    with patch("routers.health.get_health", new_callable=AsyncMock,
               return_value={"status": "ok"}):
        resp = client.get("/api/health")
    assert resp.status_code == 200


def test_health_response_has_status_key(client):
    with patch("routers.health.get_health", new_callable=AsyncMock,
               return_value={"status": "ok", "db": "connected"}):
        resp = client.get("/api/health")
    body = resp.json()
    assert "status" in body


def test_health_status_is_nonempty_string(client):
    with patch("routers.health.get_health", new_callable=AsyncMock,
               return_value={"status": "ok"}):
        resp = client.get("/api/health")
    body = resp.json()
    assert isinstance(body["status"], str)
    assert len(body["status"]) > 0


def test_health_requires_no_auth(client):
    """Health check must be reachable without any Authorization header."""
    with patch("routers.health.get_health", new_callable=AsyncMock,
               return_value={"status": "ok"}):
        resp = client.get("/api/health")  # no headers
    assert resp.status_code == 200


def test_health_is_idempotent(client):
    """Calling health three times in a row must all return 200."""
    with patch("routers.health.get_health", new_callable=AsyncMock,
               return_value={"status": "ok"}):
        for _ in range(3):
            resp = client.get("/api/health")
            assert resp.status_code == 200


def test_health_degraded_status_still_returns_200(client):
    """A degraded (but running) service must still return 200, not 5xx."""
    with patch("routers.health.get_health", new_callable=AsyncMock,
               return_value={"status": "degraded", "db": "disconnected"}):
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
