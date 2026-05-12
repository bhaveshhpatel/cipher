"""
test_rearch010_gate_config_410.py

Smoke tests for the three gate-config 410 Gone stubs added in rearch-010.

The gate_configs and gate_config_audit tables were dropped in migration 024.
The three router stubs exist to give stale API clients an actionable error
instead of a silent 404 or 500.

TODO(rearch-012): Delete this entire test file once the admin page frontend
(REARCH-012) no longer calls /gate-config at all.  The stubs in admin.py
and these tests are both safe to remove after REARCH-012 is merged and
smoke-tested in staging.

Endpoints under test:
    GET   /api/admin/gate-config
    PATCH /api/admin/gate-config
    GET   /api/admin/gate-config/history
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import get_current_user, TokenData
from routers.admin import router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_app(role: str = "admin") -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def _override():
        return TokenData(email="test@cipher.app", role=role)

    app.dependency_overrides[get_current_user] = _override
    return app


@pytest.fixture
def admin_client() -> TestClient:
    return TestClient(_make_app(role="admin"))


@pytest.fixture
def user_client() -> TestClient:
    """Non-admin role — should hit the 403 auth guard before the 410 handler."""
    return TestClient(_make_app(role="user"))


# ---------------------------------------------------------------------------
# GET /api/admin/gate-config  →  410 Gone
# ---------------------------------------------------------------------------

class TestGateConfigGet:
    def test_returns_410(self, admin_client: TestClient):
        resp = admin_client.get("/api/admin/gate-config")
        assert resp.status_code == 410

    def test_detail_mentions_migration_024(self, admin_client: TestClient):
        body = admin_client.get("/api/admin/gate-config").json()
        assert "024" in body["detail"]

    def test_detail_mentions_replacement_endpoints(self, admin_client: TestClient):
        detail = admin_client.get("/api/admin/gate-config").json()["detail"]
        # Both replacement endpoint paths must be referenced so stale clients
        # know where to go.
        assert "/api/admin/ingestion/config" in detail
        assert "/api/admin/signal-config" in detail

    def test_non_admin_gets_403_not_410(self, user_client: TestClient):
        """Auth guard fires before the 410 — non-admin must not leak endpoint existence."""
        resp = user_client.get("/api/admin/gate-config")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /api/admin/gate-config  →  410 Gone
# ---------------------------------------------------------------------------

class TestGateConfigPatch:
    def test_returns_410(self, admin_client: TestClient):
        resp = admin_client.patch(
            "/api/admin/gate-config",
            json={"gate_name": "min_premium", "tier": 1, "value": 5000.0},
        )
        assert resp.status_code == 410

    def test_returns_410_with_empty_body(self, admin_client: TestClient):
        """Body validation no longer runs — stub raises 410 unconditionally."""
        resp = admin_client.patch("/api/admin/gate-config", json={})
        assert resp.status_code == 410

    def test_detail_mentions_migration_024(self, admin_client: TestClient):
        body = admin_client.patch("/api/admin/gate-config", json={}).json()
        assert "024" in body["detail"]

    def test_non_admin_gets_403_not_410(self, user_client: TestClient):
        resp = user_client.patch("/api/admin/gate-config", json={})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/admin/gate-config/history  →  410 Gone
# ---------------------------------------------------------------------------

class TestGateConfigHistory:
    def test_returns_410(self, admin_client: TestClient):
        resp = admin_client.get("/api/admin/gate-config/history")
        assert resp.status_code == 410

    def test_detail_mentions_gate_config_audit(self, admin_client: TestClient):
        detail = admin_client.get("/api/admin/gate-config/history").json()["detail"]
        assert "gate_config_audit" in detail

    def test_detail_mentions_migration_024(self, admin_client: TestClient):
        detail = admin_client.get("/api/admin/gate-config/history").json()["detail"]
        assert "024" in detail

    def test_non_admin_gets_403_not_410(self, user_client: TestClient):
        resp = user_client.get("/api/admin/gate-config/history")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Cross-cutting: detail message is consistent across all three stubs
# ---------------------------------------------------------------------------

class TestGateConfigDetailConsistency:
    """All three stubs share _GATE_CONFIG_GONE — verify the string is identical."""

    def test_get_and_patch_detail_are_identical(self, admin_client: TestClient):
        get_detail   = admin_client.get("/api/admin/gate-config").json()["detail"]
        patch_detail = admin_client.patch("/api/admin/gate-config", json={}).json()["detail"]
        assert get_detail == patch_detail

    def test_get_and_history_detail_are_identical(self, admin_client: TestClient):
        get_detail     = admin_client.get("/api/admin/gate-config").json()["detail"]
        history_detail = admin_client.get("/api/admin/gate-config/history").json()["detail"]
        assert get_detail == history_detail
