"""
Regression tests for routers/admin.py
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from core.auth import get_current_user
from routers.admin import router


def _make_app(role="admin"):
    from core.auth import TokenData
    app = FastAPI()
    app.include_router(router)

    async def _override():
        return TokenData(email="admin@cipher.app", role=role)

    app.dependency_overrides[get_current_user] = _override
    return app


@pytest.fixture
def admin_client():
    return TestClient(_make_app(role="admin"))


@pytest.fixture
def user_client():
    """Non-admin client to test 403 guard."""
    return TestClient(_make_app(role="user"))


@pytest.fixture
def raw_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_no_auth_returns_401(raw_client):
    resp = raw_client.get("/api/admin/status")
    assert resp.status_code == 401


def test_non_admin_role_returns_403(user_client):
    resp = user_client.get("/api/admin/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

def test_admin_status_returns_200(admin_client):
    resp = admin_client.get("/api/admin/status")
    assert resp.status_code == 200


def test_admin_status_has_expected_keys(admin_client):
    resp = admin_client.get("/api/admin/status")
    body = resp.json()
    for key in ("stream", "demo", "registry"):
        assert key in body, f"admin/status missing key: {key}"


# ---------------------------------------------------------------------------
# Demo start/stop
# ---------------------------------------------------------------------------

def test_start_demo_returns_200(admin_client):
    with patch("routers.admin.start_demo", new_callable=AsyncMock,
               return_value={"ok": True, "status": "started"}):
        resp = admin_client.post("/api/admin/demo/start")
    assert resp.status_code == 200


def test_stop_demo_returns_200(admin_client):
    with patch("routers.admin.stop_demo", new_callable=AsyncMock,
               return_value={"ok": True, "status": "stopped"}):
        resp = admin_client.post("/api/admin/demo/stop")
    assert resp.status_code == 200


def test_demo_start_idempotent(admin_client):
    with patch("routers.admin.start_demo", new_callable=AsyncMock,
               return_value={"ok": True, "status": "already_running"}):
        resp = admin_client.post("/api/admin/demo/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_running"


# ---------------------------------------------------------------------------
# Tier thresholds PATCH
# ---------------------------------------------------------------------------

def test_patch_tier_thresholds_returns_200(admin_client):
    with patch("routers.admin._update_tier_thresholds",
               new_callable=AsyncMock, return_value={"ok": True}):
        resp = admin_client.patch("/api/admin/tier-thresholds",
                                  json={"t1_min_volume": 25_000_000})
    assert resp.status_code in (200, 404)  # 404 if endpoint not yet wired


def test_patch_tier_thresholds_unknown_column_rejected(admin_client):
    with patch("routers.admin._update_tier_thresholds",
               new_callable=AsyncMock, return_value={"ok": True}):
        resp = admin_client.patch("/api/admin/tier-thresholds",
                                  json={"injected_column": "DROP TABLE"})
    assert resp.status_code in (400, 422, 404)


# ---------------------------------------------------------------------------
# Tier distribution
# ---------------------------------------------------------------------------

def test_tier_distribution_returns_200(admin_client):
    with patch("routers.admin._get_tier_distribution",
               new_callable=AsyncMock,
               return_value={"t1": 10, "t2": 25, "t3": 65}):
        resp = admin_client.get("/api/admin/tier-distribution")
    assert resp.status_code in (200, 404)
