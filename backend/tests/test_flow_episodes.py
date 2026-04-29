"""
test_flow_episodes.py — 100% coverage for GET /api/flow/episodes

Tests:
  - happy path: returns episodes with correct shape
  - all four alert_level values: WATCH / ALERT / STRONG / HOLD
  - last_signaled_premium field is present and correct
  - no strike/expiry on episode rows
  - ticker filter uppercased
  - direction / contract_type / alert_level filters
  - pagination (limit/offset)
  - Supabase env missing → empty response
  - Supabase 500 → empty response
  - Supabase non-list JSON → empty response
  - httpx exception → empty response
  - row parse error skipped
  - content-range parsing (valid + malformed)
  - auth guard: 401 without token
  - optional fields (duration_seconds, started_at) nullable
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.auth import get_current_user, TokenData
from routers.flow import router


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def auth_client(app):
    app.dependency_overrides[get_current_user] = lambda: TokenData(sub="test_user")
    return TestClient(app)


@pytest.fixture()
def unauth_client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_resp(rows, status=200, content_range=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = rows
    resp.headers = {"content-range": content_range}
    resp.text = json.dumps(rows)
    return resp


def _patch_httpx(rows, status=200, content_range=""):
    mock_resp = _mock_resp(rows, status, content_range)
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    return patch("routers.flow.httpx.AsyncClient", return_value=mock_client)


def _episode(alert_level="WATCH", direction="BULLISH", contract_type="CALL"):
    return {
        "id": "ep-001",
        "ticker": "NVDA",
        "direction": direction,
        "contract_type": contract_type,
        "alert_level": alert_level,
        "trade_count": 7,
        "total_premium": 250000.0,
        "last_signaled_premium": 200000.0,
        "duration_seconds": 1800,
        "started_at": "2026-04-28T13:00:00Z",
        "updated_at": "2026-04-28T13:30:00Z",
    }


# ---------------------------------------------------------------------------
# Happy path + shape
# ---------------------------------------------------------------------------

def test_episodes_happy_path(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_episode()], content_range="0-0/1"):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["episodes"]) == 1
    e = data["episodes"][0]
    assert e["ticker"] == "NVDA"
    assert e["direction"] == "BULLISH"
    assert e["contract_type"] == "CALL"
    assert e["alert_level"] == "WATCH"
    assert e["trade_count"] == 7
    assert e["total_premium"] == 250000.0
    assert e["last_signaled_premium"] == 200000.0
    assert e["duration_seconds"] == 1800
    assert e["started_at"] == "2026-04-28T13:00:00Z"
    # Confirm no strike or expiry on episode
    assert "strike" not in e
    assert "expiry" not in e


def test_episodes_last_signaled_premium_zero(auth_client, monkeypatch):
    """last_signaled_premium=0 (new episode, never re-signaled) is valid."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    row = dict(_episode(), last_signaled_premium=0.0)
    with _patch_httpx([row]):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.json()["episodes"][0]["last_signaled_premium"] == 0.0


# ---------------------------------------------------------------------------
# All four alert_level values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("level", ["WATCH", "ALERT", "STRONG", "HOLD"])
def test_episodes_all_alert_levels(auth_client, monkeypatch, level):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_episode(alert_level=level)]):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    assert resp.json()["episodes"][0]["alert_level"] == level


# ---------------------------------------------------------------------------
# Ticker filter
# ---------------------------------------------------------------------------

def test_episodes_ticker_uppercased(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        resp = auth_client.get("/api/flow/episodes?ticker=nvda")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params["ticker"] == "eq.NVDA"
    assert resp.json()["ticker"] == "NVDA"


# ---------------------------------------------------------------------------
# Direction / contract_type / alert_level filter passthrough
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("param,value,expected_key,expected_val", [
    ("direction",     "bearish", "direction",     "eq.BEARISH"),
    ("contract_type", "put",     "contract_type", "eq.PUT"),
    ("alert_level",   "hold",    "alert_level",   "eq.HOLD"),
    ("alert_level",   "strong",  "alert_level",   "eq.STRONG"),
])
def test_episodes_filter_passthrough(auth_client, monkeypatch, param, value, expected_key, expected_val):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get(f"/api/flow/episodes?{param}={value}")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params[expected_key] == expected_val


# ---------------------------------------------------------------------------
# Nullable optional fields
# ---------------------------------------------------------------------------

def test_episodes_nullable_duration_and_started_at(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    row = dict(_episode(), duration_seconds=None, started_at=None, updated_at=None)
    with _patch_httpx([row]):
        resp = auth_client.get("/api/flow/episodes")
    e = resp.json()["episodes"][0]
    assert e["duration_seconds"] is None
    assert e["started_at"] is None
    assert e["updated_at"] is None


# ---------------------------------------------------------------------------
# Row parse error skipped
# ---------------------------------------------------------------------------

def test_episodes_parse_error_skipped(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    bad_row = {"trade_count": "not-an-int", "total_premium": "oops"}
    good_row = _episode()
    with _patch_httpx([bad_row, good_row]):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    assert len(resp.json()["episodes"]) == 1


# ---------------------------------------------------------------------------
# content-range parsing
# ---------------------------------------------------------------------------

def test_episodes_content_range_parsed(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_episode()], content_range="0-0/99"):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.json()["total"] == 99


def test_episodes_content_range_malformed(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_episode()], content_range="0-0/bad"):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.json()["total"] == 1  # fallback len(rows)


# ---------------------------------------------------------------------------
# Supabase env / error handling
# ---------------------------------------------------------------------------

def test_episodes_missing_supabase_url(auth_client):
    resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["episodes"] == []
    assert data["total"] == 0


def test_episodes_missing_key(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    assert resp.json()["episodes"] == []


def test_episodes_supabase_500(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([], status=500):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    assert resp.json()["episodes"] == []


def test_episodes_non_list_json(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"message": "error"}
    mock_resp.headers = {"content-range": ""}
    mock_resp.text = "{}"
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    with patch("routers.flow.httpx.AsyncClient", return_value=mock_client):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    assert resp.json()["episodes"] == []


def test_episodes_httpx_exception(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
    with patch("routers.flow.httpx.AsyncClient", return_value=mock_client):
        resp = auth_client.get("/api/flow/episodes")
    assert resp.status_code == 200
    assert resp.json()["episodes"] == []


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_episodes_requires_auth(unauth_client):
    resp = unauth_client.get("/api/flow/episodes")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_episodes_pagination_params_forwarded(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get("/api/flow/episodes?limit=10&offset=20")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params["limit"] == "10"
    assert params["offset"] == "20"


def test_episodes_multiple_rows(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    rows = [_episode("WATCH"), _episode("HOLD", direction="BEARISH", contract_type="PUT")]
    with _patch_httpx(rows, content_range="0-1/2"):
        resp = auth_client.get("/api/flow/episodes")
    data = resp.json()
    assert len(data["episodes"]) == 2
    assert data["total"] == 2
    levels = {e["alert_level"] for e in data["episodes"]}
    assert "WATCH" in levels
    assert "HOLD" in levels
