"""
tests/test_activity_log.py  [STORY-BE-001]

Covers:
  1. services/activity_log.log_action()  — happy path + DB failure swallowed silently
  2. services/activity_log.fetch_logs()  — delegates filters + pagination correctly
  3. GET /api/admin/activity-log         — 403 for non-admin, 200 + shape, pagination params,
                                           action filter, admin_email filter, DB error → 200 empty
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from fastapi.testclient import TestClient
from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Minimal app fixture — avoids importing the full Cipher app (heavy deps)
# ---------------------------------------------------------------------------

from routers.admin import router as admin_router
from core.auth import get_current_user, TokenData


def _make_token(role: str = "admin") -> TokenData:
    return TokenData(sub="uid-1", email=f"{role}@cipher.io", role=role)


@pytest.fixture()
def app():
    """Bare FastAPI app with only the admin router mounted."""
    _app = FastAPI()
    _app.include_router(admin_router)
    return _app


@pytest.fixture()
def admin_client(app):
    app.dependency_overrides[get_current_user] = lambda: _make_token("admin")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client(app):
    app.dependency_overrides[get_current_user] = lambda: _make_token("user")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. services/activity_log unit tests
# ---------------------------------------------------------------------------

class TestLogAction:
    @pytest.mark.asyncio
    async def test_happy_path_calls_insert(self):
        with patch("services.activity_log._insert") as mock_insert:
            from services.activity_log import log_action
            await log_action("admin@cipher.io", "demo.start", {"foo": 1}, "127.0.0.1")
        mock_insert.assert_called_once_with(
            "admin@cipher.io", "demo.start", {"foo": 1}, "127.0.0.1"
        )

    @pytest.mark.asyncio
    async def test_db_failure_is_swallowed(self):
        """A DB error must not propagate — fire-and-forget guarantee."""
        with patch("services.activity_log._insert", side_effect=RuntimeError("DB down")):
            from services.activity_log import log_action
            # Should not raise
            await log_action("admin@cipher.io", "demo.stop", {}, None)

    @pytest.mark.asyncio
    async def test_detail_defaults_to_empty_dict(self):
        with patch("services.activity_log._insert") as mock_insert:
            from services.activity_log import log_action
            await log_action("admin@cipher.io", "registry.prewarm")
        args = mock_insert.call_args[0]
        assert args[2] == {}   # detail
        assert args[3] is None  # ip


class TestFetchLogs:
    @pytest.mark.asyncio
    async def test_returns_query_result(self):
        fake_rows = [
            {"id": "abc", "action": "demo.start", "admin_email": "admin@cipher.io",
             "detail": {}, "ip_address": None, "created_at": "2026-04-30T06:00:00Z"},
        ]
        with patch("services.activity_log._query", return_value=fake_rows):
            from services.activity_log import fetch_logs
            rows = await fetch_logs(limit=10, offset=0)
        assert rows == fake_rows

    @pytest.mark.asyncio
    async def test_filters_forwarded(self):
        with patch("services.activity_log._query", return_value=[]) as mock_q:
            from services.activity_log import fetch_logs
            await fetch_logs(limit=25, offset=50, action_filter="demo.start", email_filter="a@b.com")
        mock_q.assert_called_once_with(25, 50, "demo.start", "a@b.com")


# ---------------------------------------------------------------------------
# 2. GET /api/admin/activity-log route tests
# ---------------------------------------------------------------------------

FAKE_ROWS = [
    {"id": "r1", "action": "tier_thresholds.update", "admin_email": "admin@cipher.io",
     "detail": {"updates": {"t1_min_volume": 500}}, "ip_address": "10.0.0.1",
     "created_at": "2026-04-30T07:00:00Z"},
    {"id": "r2", "action": "demo.start", "admin_email": "admin@cipher.io",
     "detail": {}, "ip_address": None,
     "created_at": "2026-04-30T06:00:00Z"},
]


class TestGetActivityLogEndpoint:
    def test_403_for_non_admin(self, anon_client):
        with patch("routers.admin.fetch_logs", return_value=FAKE_ROWS):
            r = anon_client.get("/api/admin/activity-log")
        assert r.status_code == 403

    def test_200_default_params(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=FAKE_ROWS) as mock_fl:
            r = admin_client.get("/api/admin/activity-log")
        assert r.status_code == 200
        body = r.json()
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert body["count"] == 2
        assert len(body["items"]) == 2
        mock_fl.assert_awaited_once_with(
            limit=50, offset=0, action_filter=None, email_filter=None
        )

    def test_pagination_params_forwarded(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=[]) as mock_fl:
            r = admin_client.get("/api/admin/activity-log?limit=10&offset=20")
        assert r.status_code == 200
        mock_fl.assert_awaited_once_with(
            limit=10, offset=20, action_filter=None, email_filter=None
        )

    def test_action_filter_forwarded(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=[FAKE_ROWS[0]]) as mock_fl:
            r = admin_client.get("/api/admin/activity-log?action=tier_thresholds.update")
        assert r.status_code == 200
        mock_fl.assert_awaited_once_with(
            limit=50, offset=0, action_filter="tier_thresholds.update", email_filter=None
        )

    def test_admin_email_filter_forwarded(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=[]) as mock_fl:
            r = admin_client.get("/api/admin/activity-log?admin_email=other@cipher.io")
        assert r.status_code == 200
        mock_fl.assert_awaited_once_with(
            limit=50, offset=0, action_filter=None, email_filter="other@cipher.io"
        )

    def test_limit_upper_bound_enforced(self, admin_client):
        """limit > 200 should return 422."""
        with patch("routers.admin.fetch_logs", return_value=[]):
            r = admin_client.get("/api/admin/activity-log?limit=201")
        assert r.status_code == 422

    def test_limit_lower_bound_enforced(self, admin_client):
        """limit=0 should return 422."""
        with patch("routers.admin.fetch_logs", return_value=[]):
            r = admin_client.get("/api/admin/activity-log?limit=0")
        assert r.status_code == 422

    def test_fetch_error_returns_500(self, admin_client):
        """If fetch_logs raises unexpectedly, FastAPI returns 500."""
        async def _boom(**_):
            raise RuntimeError("DB down")
        with patch("routers.admin.fetch_logs", side_effect=RuntimeError("DB down")):
            r = admin_client.get("/api/admin/activity-log")
        assert r.status_code == 500

    def test_response_shape(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=FAKE_ROWS):
            r = admin_client.get("/api/admin/activity-log")
        body = r.json()
        assert set(body.keys()) == {"limit", "offset", "count", "items"}
        item = body["items"][0]
        assert "action" in item
        assert "admin_email" in item
        assert "created_at" in item
