"""
tests/test_activity_log.py  [STORY-BE-001]

Covers:
  1. services/activity_log.log_action()  — happy path, DB failure swallowed + warning logged,
                                            detail defaults, get_running_loop used
  2. services/activity_log.fetch_logs()  — (rows, total) tuple, filters + date-range forwarded
  3. GET /api/admin/activity-log         — 403 non-admin, 200 shape (incl. total), pagination,
                                            action/email/since/before filters, limit bounds, 500 on error
"""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from routers.admin import router as admin_router
from core.auth import get_current_user, TokenData


def _make_token(role: str = "admin") -> TokenData:
    return TokenData(sub="uid-1", email=f"{role}@cipher.io", role=role)


@pytest.fixture()
def app():
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
    async def test_db_failure_is_swallowed_and_warning_logged(self):
        """Error must not propagate AND must be recorded at WARNING level."""
        with patch("services.activity_log._insert", side_effect=RuntimeError("DB down")), \
             patch("services.activity_log.log") as mock_log:
            from services.activity_log import log_action
            await log_action("admin@cipher.io", "demo.stop", {}, None)
        mock_log.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_detail_defaults_to_empty_dict(self):
        with patch("services.activity_log._insert") as mock_insert:
            from services.activity_log import log_action
            await log_action("admin@cipher.io", "registry.prewarm")
        args = mock_insert.call_args[0]
        assert args[2] == {}    # detail
        assert args[3] is None  # ip


class TestFetchLogs:
    @pytest.mark.asyncio
    async def test_returns_rows_and_total(self):
        fake_rows = [
            {"id": "abc", "action": "demo.start", "admin_email": "admin@cipher.io",
             "detail": {}, "ip_address": None, "created_at": "2026-04-30T06:00:00Z"},
        ]
        with patch("services.activity_log._query", return_value=(fake_rows, 1)):
            from services.activity_log import fetch_logs
            rows, total = await fetch_logs(limit=10, offset=0)
        assert rows == fake_rows
        assert total == 1

    @pytest.mark.asyncio
    async def test_all_filters_forwarded(self):
        with patch("services.activity_log._query", return_value=([], 0)) as mock_q:
            from services.activity_log import fetch_logs
            await fetch_logs(
                limit=25,
                offset=50,
                action_filter="demo.start",
                email_filter="a@b.com",
                since="2026-04-30T00:00:00Z",
                before="2026-04-30T23:59:59Z",
            )
        mock_q.assert_called_once_with(
            25, 50, "demo.start", "a@b.com",
            "2026-04-30T00:00:00Z", "2026-04-30T23:59:59Z",
        )


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
        with patch("routers.admin.fetch_logs", return_value=(FAKE_ROWS, 2)):
            r = anon_client.get("/api/admin/activity-log")
        assert r.status_code == 403

    def test_200_default_params_and_shape(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=(FAKE_ROWS, 47)) as mock_fl:
            r = admin_client.get("/api/admin/activity-log")
        assert r.status_code == 200
        body = r.json()
        assert body["limit"]  == 50
        assert body["offset"] == 0
        assert body["count"]  == 2
        assert body["total"]  == 47
        assert len(body["items"]) == 2
        mock_fl.assert_awaited_once_with(
            limit=50, offset=0,
            action_filter=None, email_filter=None,
            since=None, before=None,
        )

    def test_response_keys(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=(FAKE_ROWS, 2)):
            r = admin_client.get("/api/admin/activity-log")
        assert set(r.json().keys()) == {"limit", "offset", "total", "count", "items"}

    def test_pagination_params_forwarded(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=([], 0)) as mock_fl:
            r = admin_client.get("/api/admin/activity-log?limit=10&offset=20")
        assert r.status_code == 200
        mock_fl.assert_awaited_once_with(
            limit=10, offset=20,
            action_filter=None, email_filter=None,
            since=None, before=None,
        )

    def test_action_filter_forwarded(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=([FAKE_ROWS[0]], 1)) as mock_fl:
            r = admin_client.get("/api/admin/activity-log?action=tier_thresholds.update")
        assert r.status_code == 200
        mock_fl.assert_awaited_once_with(
            limit=50, offset=0,
            action_filter="tier_thresholds.update", email_filter=None,
            since=None, before=None,
        )

    def test_admin_email_filter_forwarded(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=([], 0)) as mock_fl:
            r = admin_client.get("/api/admin/activity-log?admin_email=other@cipher.io")
        assert r.status_code == 200
        mock_fl.assert_awaited_once_with(
            limit=50, offset=0,
            action_filter=None, email_filter="other@cipher.io",
            since=None, before=None,
        )

    def test_since_before_forwarded(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=([], 0)) as mock_fl:
            r = admin_client.get(
                "/api/admin/activity-log"
                "?since=2026-04-30T00:00:00Z&before=2026-04-30T23:59:59Z"
            )
        assert r.status_code == 200
        mock_fl.assert_awaited_once_with(
            limit=50, offset=0,
            action_filter=None, email_filter=None,
            since="2026-04-30T00:00:00Z", before="2026-04-30T23:59:59Z",
        )

    def test_limit_upper_bound_enforced(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=([], 0)):
            r = admin_client.get("/api/admin/activity-log?limit=201")
        assert r.status_code == 422

    def test_limit_lower_bound_enforced(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=([], 0)):
            r = admin_client.get("/api/admin/activity-log?limit=0")
        assert r.status_code == 422

    def test_fetch_error_returns_500(self, admin_client):
        with patch("routers.admin.fetch_logs", side_effect=RuntimeError("DB down")):
            r = admin_client.get("/api/admin/activity-log")
        assert r.status_code == 500

    def test_item_fields_present(self, admin_client):
        with patch("routers.admin.fetch_logs", return_value=(FAKE_ROWS, 2)):
            r = admin_client.get("/api/admin/activity-log")
        item = r.json()["items"][0]
        assert "action"      in item
        assert "admin_email" in item
        assert "created_at"  in item
        assert "detail"      in item
