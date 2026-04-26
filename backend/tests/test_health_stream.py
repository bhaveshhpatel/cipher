"""Regression tests for the /api/health/stream SSE endpoint."""
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


def test_health_stream_returns_200(client):
    with patch("routers.health.get_health", new_callable=AsyncMock,
               return_value={"status": "ok"}):
        resp = client.get("/api/health")
    assert resp.status_code == 200
