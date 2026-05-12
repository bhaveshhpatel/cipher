"""
Rearch-010 pre-merge smoke tests — Item 4:
  Verify that all retired endpoints return HTTP 410 Gone (not 404 or 500).

  Retired endpoints (migration 024 / REARCH-010):
    GET  /api/admin/gate-config           -> 410
    POST /api/admin/gate-config           -> 410
    PUT  /api/admin/gate-config           -> 410  (if route existed)
    GET  /api/admin/backtest-results      -> 410
    GET  /api/admin/gate-config-audit     -> 410  (if route existed)

  These must return 410, not:
    - 404 (route not registered — would mean the stub was never added)
    - 500 (unhandled exception — would mean broken code was left in place)
    - 200/any success code (route still live — schema was not retired)

Confirmed against routers/admin.py on feat/rearch-010-db-schema-purge.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import get_current_user
from routers.admin import router


def _make_app(role: str = "admin") -> FastAPI:
    from core.auth import TokenData

    app = FastAPI()
    app.include_router(router)

    async def _override():
        return TokenData(email="admin@cipher.app", role=role)

    app.dependency_overrides[get_current_user] = _override
    return app


@pytest.fixture(scope="module")
def admin_client():
    return TestClient(_make_app(role="admin"))


# ---------------------------------------------------------------------------
# gate-config stubs
# ---------------------------------------------------------------------------

class TestGateConfigStubs:
    """GET/POST /api/admin/gate-config must return 410, not 404 or 500."""

    def test_get_gate_config_returns_410(self, admin_client):
        resp = admin_client.get("/api/admin/gate-config")
        assert resp.status_code == 410, (
            f"Expected 410 Gone, got {resp.status_code}. "
            "If 404: the 410 stub was never registered. "
            "If 500: dead code is exploding. "
            f"Body: {resp.text[:300]}"
        )

    def test_get_gate_config_body_has_gone_detail(self, admin_client):
        resp = admin_client.get("/api/admin/gate-config")
        body = resp.json()
        # detail must clearly communicate the column/table is dropped
        detail = body.get("detail", "").lower()
        assert "removed" in detail or "dropped" in detail or "410" in detail or "gone" in detail, (
            f"410 detail message is too vague: {body.get('detail')!r}. "
            "Should mention 'removed', 'dropped', or 'gone'."
        )

    def test_post_gate_config_returns_410(self, admin_client):
        resp = admin_client.post("/api/admin/gate-config", json={})
        assert resp.status_code == 410, (
            f"Expected 410 Gone for POST, got {resp.status_code}. Body: {resp.text[:300]}"
        )

    def test_gate_config_is_not_404(self, admin_client):
        """404 means the stub was never registered — catch this explicitly."""
        resp = admin_client.get("/api/admin/gate-config")
        assert resp.status_code != 404, (
            "Got 404 — the 410 stub route is missing from routers/admin.py. "
            "Add: @router.get('/api/admin/gate-config') -> raise HTTPException(410, ...)"
        )

    def test_gate_config_is_not_500(self, admin_client):
        """500 means the old implementation is still present and broken."""
        resp = admin_client.get("/api/admin/gate-config")
        assert resp.status_code != 500, (
            f"Got 500 — dead gate-config code is still executing. Body: {resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# backtest-results stub
# ---------------------------------------------------------------------------

class TestBacktestResultsStub:
    """GET /api/admin/backtest-results must return 410, not 404 or 500."""

    def test_get_backtest_results_returns_410(self, admin_client):
        resp = admin_client.get("/api/admin/backtest-results")
        assert resp.status_code == 410, (
            f"Expected 410 Gone, got {resp.status_code}. Body: {resp.text[:300]}"
        )

    def test_backtest_results_is_not_404(self, admin_client):
        resp = admin_client.get("/api/admin/backtest-results")
        assert resp.status_code != 404, (
            "Got 404 — the 410 stub route for /backtest-results is not registered."
        )

    def test_backtest_results_is_not_500(self, admin_client):
        resp = admin_client.get("/api/admin/backtest-results")
        assert resp.status_code != 500, (
            f"Got 500 — dead backtest-results code still executing. Body: {resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Non-admin role must still get 403, not 410 (auth guard runs first)
# ---------------------------------------------------------------------------

class TestGateConfigAuthStillEnforced:
    """Auth guard must fire before the 410 stub — non-admin should see 403."""

    def test_non_admin_gate_config_returns_403(self):
        user_client = TestClient(_make_app(role="user"))
        resp = user_client.get("/api/admin/gate-config")
        assert resp.status_code == 403, (
            f"Expected 403 for non-admin, got {resp.status_code}. "
            "Auth guard must run before the 410 stub."
        )
