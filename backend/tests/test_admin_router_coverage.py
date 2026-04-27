"""
Coverage boost for routers/admin.py — targets the branches
that test_admin_router.py misses:

  - ingestion GET / PATCH routes
  - tier-thresholds GET: no service_key (500) + no active row (404)
  - tier-thresholds PATCH: no service_key (500) + empty updates (422)
  - tier-distribution: no service_key (500) + no snapshot (404)
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock

from core.auth import get_current_user, TokenData
from routers.admin import router


def _make_app(role: str = "admin") -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def _override():
        return TokenData(email="admin@cipher.app", role=role)

    app.dependency_overrides[get_current_user] = _override
    return app


@pytest.fixture
def client():
    return TestClient(_make_app())


# ---------------------------------------------------------------------------
# Ingestion config routes
# ---------------------------------------------------------------------------

def test_get_ingestion_config(client):
    with patch("services.ingestion_config.get_all_rows", new=AsyncMock(return_value=[])):
        resp = client.get("/api/admin/ingestion/config")
    assert resp.status_code == 200
    assert "config" in resp.json()


def test_patch_ingestion_config_ok(client):
    with patch("services.ingestion_config.update_config", new=AsyncMock(return_value=True)):
        resp = client.patch(
            "/api/admin/ingestion/config",
            json={"key": "max_premium", "value": "500000"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["key"] == "max_premium"


def test_patch_ingestion_config_fail_500(client):
    with patch("services.ingestion_config.update_config", new=AsyncMock(return_value=False)):
        resp = client.patch(
            "/api/admin/ingestion/config",
            json={"key": "bad_key", "value": "0"},
        )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Tier thresholds GET — missing key / no row
# ---------------------------------------------------------------------------

def test_get_tier_thresholds_no_service_key(client):
    from config import settings
    with patch.object(settings, "SUPABASE_SERVICE_KEY", None):
        resp = client.get("/api/admin/tier-thresholds")
    assert resp.status_code == 500


def test_get_tier_thresholds_no_active_row(client):
    from config import settings
    mock_sb = MagicMock()
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.order.return_value = q
    q.limit.return_value = q
    q.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value = q
    with patch.object(settings, "SUPABASE_SERVICE_KEY", "fake"), \
         patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch("supabase.create_client", return_value=mock_sb):
        resp = client.get("/api/admin/tier-thresholds")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tier thresholds PATCH — edge cases
# ---------------------------------------------------------------------------

def test_patch_tier_thresholds_no_service_key(client):
    from config import settings
    with patch.object(settings, "SUPABASE_SERVICE_KEY", None):
        resp = client.patch(
            "/api/admin/tier-thresholds",
            json={"updates": {"t1_min_volume": 1000}},
        )
    assert resp.status_code == 500


def test_patch_tier_thresholds_empty_updates(client):
    resp = client.patch("/api/admin/tier-thresholds", json={"updates": {}})
    assert resp.status_code == 422


def test_patch_tier_thresholds_no_active_row_404(client):
    from config import settings
    mock_sb = MagicMock()
    q = MagicMock()
    q.update.return_value = q
    q.eq.return_value = q
    q.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value = q
    with patch.object(settings, "SUPABASE_SERVICE_KEY", "fake"), \
         patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch("supabase.create_client", return_value=mock_sb), \
         patch("routers.admin.te.invalidate_thresholds_cache"), \
         patch("routers.admin.te.invalidate_cache"):
        resp = client.patch(
            "/api/admin/tier-thresholds",
            json={"updates": {"t1_min_volume": 1000}},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tier distribution — edge cases
# ---------------------------------------------------------------------------

def test_get_tier_distribution_no_service_key(client):
    from config import settings
    with patch.object(settings, "SUPABASE_SERVICE_KEY", None):
        resp = client.get("/api/admin/tier-distribution")
    assert resp.status_code == 500


def test_get_tier_distribution_no_snapshot(client):
    from config import settings
    mock_sb = MagicMock()
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.order.return_value = q
    q.limit.return_value = q
    q.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value = q
    with patch.object(settings, "SUPABASE_SERVICE_KEY", "fake"), \
         patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch("supabase.create_client", return_value=mock_sb):
        resp = client.get("/api/admin/tier-distribution")
    assert resp.status_code == 404


def test_get_tier_distribution_unknown_tier_falls_to_3(client):
    from config import settings
    mock_sb = MagicMock()
    snap_q = MagicMock()
    snap_q.select.return_value = snap_q
    snap_q.eq.return_value = snap_q
    snap_q.order.return_value = snap_q
    snap_q.limit.return_value = snap_q
    snap_q.execute.side_effect = [
        MagicMock(data=[{"id": 99}]),
        MagicMock(data=[
            {"symbol": "XYZ", "tier": 99, "open_interest": 100},
        ]),
    ]
    mock_sb.table.return_value = snap_q
    with patch.object(settings, "SUPABASE_SERVICE_KEY", "fake"), \
         patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch("supabase.create_client", return_value=mock_sb):
        resp = client.get("/api/admin/tier-distribution")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tiers"]["3"]["count"] == 1
