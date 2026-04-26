"""
Regression tests for routers/admin.py

Fix summary (2026-04-26):
  - Route paths corrected to match admin.py:
      /api/admin/status       → /api/admin/demo/status
      /api/admin/demo/start   → /api/admin/demo/on
      /api/admin/demo/stop    → /api/admin/demo/off
  - Patch targets corrected: start_demo / stop_demo are imported inside
    router functions, so patch at the source module:
      services.demo_engine.start_demo / stop_demo
  - PATCH tier-thresholds uses body {"updates": {...}} to match
    TierThresholdUpdate pydantic model
  - PATCH with unknown column sends {"updates": {"injected_column": ...}}
  - SUPABASE_SERVICE_KEY patched so GET tier-thresholds doesn't 500
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock

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
    # Without dependency override, get_current_user raises 401
    resp = raw_client.get("/api/admin/demo/status")
    assert resp.status_code == 401


def test_non_admin_role_returns_403(user_client):
    resp = user_client.get("/api/admin/demo/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Status endpoint  (GET /api/admin/demo/status)
# ---------------------------------------------------------------------------

def test_admin_status_returns_200(admin_client):
    with patch("services.demo_engine.get_stats", return_value={"running": False, "events_sent": 0}):
        resp = admin_client.get("/api/admin/demo/status")
    assert resp.status_code == 200


def test_admin_status_has_expected_keys(admin_client):
    with patch("services.demo_engine.get_stats", return_value={"running": False, "events_sent": 0}):
        resp = admin_client.get("/api/admin/demo/status")
    body = resp.json()
    # Response is {"demo": {...}, "admin": ..., "role": ...}
    assert "demo" in body
    assert "admin" in body


# ---------------------------------------------------------------------------
# Demo start/stop  (POST /api/admin/demo/on|off)
# ---------------------------------------------------------------------------

def test_start_demo_returns_200(admin_client):
    with patch("services.demo_engine.start_demo", new=AsyncMock(return_value={"ok": True, "status": "started"})):
        resp = admin_client.post("/api/admin/demo/on")
    assert resp.status_code == 200


def test_stop_demo_returns_200(admin_client):
    with patch("services.demo_engine.stop_demo", new=AsyncMock(return_value={"ok": True, "status": "stopped"})):
        resp = admin_client.post("/api/admin/demo/off")
    assert resp.status_code == 200


def test_demo_start_idempotent(admin_client):
    with patch("services.demo_engine.start_demo",
               new=AsyncMock(return_value={"ok": True, "status": "already_running"})):
        resp = admin_client.post("/api/admin/demo/on")
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_running"


# ---------------------------------------------------------------------------
# Tier thresholds PATCH  (PATCH /api/admin/tier-thresholds)
# Body must be {"updates": {<col>: <value>}}
# ---------------------------------------------------------------------------

def _patch_supabase_for_tier():
    """Context manager that stubs out SUPABASE_SERVICE_KEY + supabase client."""
    from config import settings
    mock_sb = MagicMock()
    mock_tbl = MagicMock()
    mock_tbl.update.return_value = mock_tbl
    mock_tbl.eq.return_value     = mock_tbl
    mock_tbl.execute.return_value = MagicMock(data=[{"id": 1, "t1_min_volume": 25_000_000}])
    mock_sb.table.return_value = mock_tbl
    return (
        patch.object(settings, "SUPABASE_SERVICE_KEY", "fake-key"),
        patch.object(settings, "SUPABASE_URL",         "http://fake"),
        patch("routers.admin.te.invalidate_thresholds_cache"),
        patch("supabase.create_client", return_value=mock_sb),
    )


def test_patch_tier_thresholds_returns_200(admin_client):
    patches = _patch_supabase_for_tier()
    with patches[0], patches[1], patches[2], patches[3]:
        resp = admin_client.patch(
            "/api/admin/tier-thresholds",
            json={"updates": {"t1_min_volume": 25_000_000}},
        )
    assert resp.status_code == 200


def test_patch_tier_thresholds_unknown_column_rejected(admin_client):
    resp = admin_client.patch(
        "/api/admin/tier-thresholds",
        json={"updates": {"injected_column": "DROP TABLE"}},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tier distribution  (GET /api/admin/tier-distribution)
# ---------------------------------------------------------------------------

def test_tier_distribution_returns_200(admin_client):
    mock_sb = MagicMock()
    snap_q  = MagicMock()
    snap_q.select.return_value = snap_q
    snap_q.eq.return_value     = snap_q
    snap_q.order.return_value  = snap_q
    snap_q.limit.return_value  = snap_q
    snap_q.execute.side_effect = [
        MagicMock(data=[{"id": 42}]),   # snapshot query
        MagicMock(data=[
            {"symbol": "SPY",  "tier": 1, "open_interest": 50000},
            {"symbol": "HOOD", "tier": 2, "open_interest": 800},
        ]),
    ]
    mock_sb.table.return_value = snap_q
    from config import settings
    with patch.object(settings, "SUPABASE_SERVICE_KEY", "fake-key"), \
         patch.object(settings, "SUPABASE_URL",         "http://fake"), \
         patch("supabase.create_client", return_value=mock_sb):
        resp = admin_client.get("/api/admin/tier-distribution")
    assert resp.status_code == 200
    body = resp.json()
    assert "tiers" in body
    assert "total" in body
