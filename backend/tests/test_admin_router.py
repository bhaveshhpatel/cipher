"""
Regression tests for routers/admin.py

Strategy:
  - All routes use _require_admin which calls get_current_user (JWT) then
    checks role == 'admin'. We override the dependency at the app level.
  - Supabase calls inside endpoints are patched via unittest.mock so no
    live DB is needed.
  - Demo engine calls are patched to avoid side-effects.

Covers:
  _require_admin:
  - User with role='user' gets 403 on every protected endpoint
  - User with role='admin' passes the guard
  - 403 response body contains 'Admin access required'

  _ALLOWED_TIER_COLUMNS whitelist:
  - All 15 expected column names are present
  - All t1/t2/t3 prefixed names are present

  GET /api/admin/demo/status:
  - Returns dict with 'demo', 'admin', 'role' keys
  - 'admin' field contains the email from TokenData

  POST /api/admin/demo/on:
  - Calls start_demo() and returns its result
  - Returns {ok: True, status: 'started'} or 'already_running'

  POST /api/admin/demo/off:
  - Calls stop_demo() and returns its result
  - Returns {ok: True, status: 'stopped'} or 'already_stopped'

  GET /api/admin/ingestion/config:
  - Returns {config: <list>}
  - Works when get_all_rows returns []

  PATCH /api/admin/ingestion/config:
  - Success: returns {ok: True, key: ..., value: ...}
  - Failure (update_config returns False): returns 500

  PATCH /api/admin/tier-thresholds:
  - Unknown column in updates → 422
  - Empty updates dict → 422
  - No SUPABASE_SERVICE_KEY → 500

  GET /api/admin/tier-distribution:
  - No SUPABASE_SERVICE_KEY → 500
"""
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from unittest.mock import patch, AsyncMock, MagicMock

from core.auth import get_current_user, TokenData
from routers.admin import router, _ALLOWED_TIER_COLUMNS


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

def _make_app(role: str = "admin") -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def _override_user():
        return TokenData(email="test@cipher.app", role=role)

    app.dependency_overrides[get_current_user] = _override_user
    return app


@pytest.fixture
def admin_client():
    return TestClient(_make_app(role="admin"))


@pytest.fixture
def user_client():
    return TestClient(_make_app(role="user"))


# ---------------------------------------------------------------------------
# _ALLOWED_TIER_COLUMNS whitelist
# ---------------------------------------------------------------------------

def test_allowed_tier_columns_is_set():
    assert isinstance(_ALLOWED_TIER_COLUMNS, set)


def test_allowed_tier_columns_has_15_entries():
    assert len(_ALLOWED_TIER_COLUMNS) == 15


@pytest.mark.parametrize("col", [
    "t1_min_volume", "t1_min_last_price", "t1_min_oi", "t1_atm_pct", "t1_max_dte",
    "t2_min_volume", "t2_min_last_price", "t2_min_oi", "t2_atm_pct", "t2_max_dte",
    "t3_min_volume", "t3_min_last_price", "t3_min_oi", "t3_atm_pct", "t3_max_dte",
])
def test_allowed_tier_columns_contains_expected(col):
    assert col in _ALLOWED_TIER_COLUMNS


# ---------------------------------------------------------------------------
# _require_admin: non-admin gets 403
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET",   "/api/admin/demo/status"),
    ("POST",  "/api/admin/demo/on"),
    ("POST",  "/api/admin/demo/off"),
    ("GET",   "/api/admin/ingestion/config"),
    ("PATCH", "/api/admin/ingestion/config"),
    ("GET",   "/api/admin/tier-thresholds"),
    ("PATCH", "/api/admin/tier-thresholds"),
    ("GET",   "/api/admin/tier-distribution"),
])
def test_non_admin_gets_403(user_client, method, path):
    resp = getattr(user_client, method.lower())(path, json={})
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/admin/demo/status
# ---------------------------------------------------------------------------

def test_demo_status_returns_expected_keys(admin_client):
    mock_stats = {"running": False, "ticks_emitted": 0, "signals_emitted": 0, "errors": 0}
    with patch("routers.admin.services.demo_engine", create=True), \
         patch("services.demo_engine.get_stats", return_value=mock_stats):
        resp = admin_client.get("/api/admin/demo/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "demo" in body
    assert "admin" in body
    assert "role"  in body


def test_demo_status_admin_email_present(admin_client):
    mock_stats = {"running": False, "ticks_emitted": 0, "signals_emitted": 0, "errors": 0}
    with patch("services.demo_engine.get_stats", return_value=mock_stats):
        resp = admin_client.get("/api/admin/demo/status")
    assert resp.json()["admin"] == "test@cipher.app"


# ---------------------------------------------------------------------------
# POST /api/admin/demo/on
# ---------------------------------------------------------------------------

def test_demo_on_returns_started(admin_client):
    with patch("services.demo_engine.start_demo", new_callable=AsyncMock,
               return_value={"ok": True, "status": "started"}):
        resp = admin_client.post("/api/admin/demo/on")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "started"}


def test_demo_on_idempotent_already_running(admin_client):
    with patch("services.demo_engine.start_demo", new_callable=AsyncMock,
               return_value={"ok": True, "status": "already_running"}):
        resp = admin_client.post("/api/admin/demo/on")
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_running"


# ---------------------------------------------------------------------------
# POST /api/admin/demo/off
# ---------------------------------------------------------------------------

def test_demo_off_returns_stopped(admin_client):
    with patch("services.demo_engine.stop_demo", new_callable=AsyncMock,
               return_value={"ok": True, "status": "stopped"}):
        resp = admin_client.post("/api/admin/demo/off")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "stopped"}


def test_demo_off_idempotent_already_stopped(admin_client):
    with patch("services.demo_engine.stop_demo", new_callable=AsyncMock,
               return_value={"ok": True, "status": "already_stopped"}):
        resp = admin_client.post("/api/admin/demo/off")
    assert resp.json()["status"] == "already_stopped"


# ---------------------------------------------------------------------------
# GET /api/admin/ingestion/config
# ---------------------------------------------------------------------------

def test_get_ingestion_config_returns_config_key(admin_client):
    with patch("services.ingestion_config.get_all_rows", new_callable=AsyncMock,
               return_value=[]):
        resp = admin_client.get("/api/admin/ingestion/config")
    assert resp.status_code == 200
    assert "config" in resp.json()


def test_get_ingestion_config_rows_passed_through(admin_client):
    rows = [{"key": "REGISTRY_MAX_DTE", "value": "90", "value_type": "int"}]
    with patch("services.ingestion_config.get_all_rows", new_callable=AsyncMock,
               return_value=rows):
        resp = admin_client.get("/api/admin/ingestion/config")
    assert resp.json()["config"] == rows


# ---------------------------------------------------------------------------
# PATCH /api/admin/ingestion/config
# ---------------------------------------------------------------------------

def test_patch_ingestion_config_success(admin_client):
    with patch("services.ingestion_config.update_config", new_callable=AsyncMock,
               return_value=True):
        resp = admin_client.patch(
            "/api/admin/ingestion/config",
            json={"key": "REGISTRY_MAX_DTE", "value": "60"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["key"] == "REGISTRY_MAX_DTE"
    assert body["value"] == "60"


def test_patch_ingestion_config_failure_returns_500(admin_client):
    with patch("services.ingestion_config.update_config", new_callable=AsyncMock,
               return_value=False):
        resp = admin_client.patch(
            "/api/admin/ingestion/config",
            json={"key": "REGISTRY_MAX_DTE", "value": "60"},
        )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# PATCH /api/admin/tier-thresholds: validation
# ---------------------------------------------------------------------------

def test_patch_tier_thresholds_unknown_column_returns_422(admin_client):
    resp = admin_client.patch(
        "/api/admin/tier-thresholds",
        json={"updates": {"not_a_real_column": 100}},
    )
    assert resp.status_code == 422
    assert "Unknown threshold column" in resp.json()["detail"]


def test_patch_tier_thresholds_empty_updates_returns_422(admin_client):
    resp = admin_client.patch(
        "/api/admin/tier-thresholds",
        json={"updates": {}},
    )
    assert resp.status_code == 422


def test_patch_tier_thresholds_no_service_key_returns_500(admin_client):
    """Valid columns but missing SUPABASE_SERVICE_KEY → 500."""
    with patch("routers.admin.settings") as ms:
        ms.SUPABASE_SERVICE_KEY = ""
        resp = admin_client.patch(
            "/api/admin/tier-thresholds",
            json={"updates": {"t1_min_volume": 1000}},
        )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/admin/tier-distribution: no service key → 500
# ---------------------------------------------------------------------------

def test_get_tier_distribution_no_service_key_returns_500(admin_client):
    with patch("routers.admin.settings") as ms:
        ms.SUPABASE_SERVICE_KEY = ""
        resp = admin_client.get("/api/admin/tier-distribution")
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/admin/tier-thresholds: no service key → 500
# ---------------------------------------------------------------------------

def test_get_tier_thresholds_no_service_key_returns_500(admin_client):
    with patch("routers.admin.settings") as ms:
        ms.SUPABASE_SERVICE_KEY = ""
        resp = admin_client.get("/api/admin/tier-thresholds")
    assert resp.status_code == 500
