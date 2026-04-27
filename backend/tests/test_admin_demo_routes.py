"""
P1 tests for the demo_status / demo_on / demo_off routes in routers/admin.py.

The routes use lazy imports inside function bodies:

    @router.get("/demo/status")
    async def demo_status(...):
        from services.demo_engine import get_stats   # imported HERE
        ...

Patching at the module level BEFORE the router is included has no effect
because Python caches the import on first call.  The correct strategy:

  1. Pre-import routers.admin so the lazy import is triggered.
  2. Patch 'routers.admin.get_stats' (the name bound in the router module
     namespace after the first resolution).

But since these are inside function bodies (not module-level), the name is
not bound until the function executes.  We therefore patch the source module
(services.demo_engine) directly so it intercepts at import time:

    with patch('services.demo_engine.get_stats', ...):
        client.get(...)

This is the only reliable approach for function-body lazy imports.
"""
import pytest
from unittest.mock import patch, AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import get_current_user, TokenData
from routers.admin import router

# Pre-import demo_engine so the module is in sys.modules before we patch it
import services.demo_engine as _demo_engine_preload  # noqa: F401


def _make_app(role: str = "admin") -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def _override():
        return TokenData(email="admin@cipher.app", role=role)

    app.dependency_overrides[get_current_user] = _override
    return app


@pytest.fixture
def admin_client():
    return TestClient(_make_app())


@pytest.fixture
def user_client():
    """Non-admin client — should get 403 on all demo routes."""
    return TestClient(_make_app(role="user"), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/admin/demo/status
# ---------------------------------------------------------------------------

def test_demo_status_returns_200(admin_client):
    fake_stats = {"running": False, "events_sent": 0}
    with patch("services.demo_engine.get_stats", return_value=fake_stats):
        resp = admin_client.get("/api/admin/demo/status")
    assert resp.status_code == 200


def test_demo_status_response_contains_demo_key(admin_client):
    fake_stats = {"running": False, "events_sent": 42}
    with patch("services.demo_engine.get_stats", return_value=fake_stats):
        resp = admin_client.get("/api/admin/demo/status")
    body = resp.json()
    assert "demo" in body
    assert body["demo"]["events_sent"] == 42


def test_demo_status_response_contains_admin_email(admin_client):
    with patch("services.demo_engine.get_stats", return_value={}):
        resp = admin_client.get("/api/admin/demo/status")
    assert resp.json().get("admin") == "admin@cipher.app"


def test_demo_status_forbidden_for_non_admin(user_client):
    resp = user_client.get("/api/admin/demo/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/admin/demo/on
# ---------------------------------------------------------------------------

def test_demo_on_returns_200(admin_client):
    with patch("services.demo_engine.start_demo",
               new=AsyncMock(return_value={"ok": True, "status": "started"})):
        resp = admin_client.post("/api/admin/demo/on")
    assert resp.status_code == 200


def test_demo_on_response_body(admin_client):
    payload = {"ok": True, "status": "started"}
    with patch("services.demo_engine.start_demo",
               new=AsyncMock(return_value=payload)):
        resp = admin_client.post("/api/admin/demo/on")
    assert resp.json()["ok"] is True


def test_demo_on_forbidden_for_non_admin(user_client):
    resp = user_client.post("/api/admin/demo/on")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/admin/demo/off
# ---------------------------------------------------------------------------

def test_demo_off_returns_200(admin_client):
    with patch("services.demo_engine.stop_demo",
               new=AsyncMock(return_value={"ok": True, "status": "stopped"})):
        resp = admin_client.post("/api/admin/demo/off")
    assert resp.status_code == 200


def test_demo_off_response_body(admin_client):
    payload = {"ok": True, "status": "stopped"}
    with patch("services.demo_engine.stop_demo",
               new=AsyncMock(return_value=payload)):
        resp = admin_client.post("/api/admin/demo/off")
    assert resp.json()["ok"] is True


def test_demo_off_forbidden_for_non_admin(user_client):
    resp = user_client.post("/api/admin/demo/off")
    assert resp.status_code == 403
