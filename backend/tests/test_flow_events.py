"""
test_flow_events.py — 100% coverage for GET /api/flow/events

Tests:
  - happy path: returns events list with correct shape
  - ticker filter uppercased and passed through
  - sentiment / contract_type / tier filters
  - aggressive=True / golden_sweep=True boolean filters
  - combined filters build correct active_filters dict
  - row parse error is skipped (warning logged, rest returned)
  - Supabase env missing → 200 with empty events
  - Supabase returns non-200 → 200 with empty events
  - Supabase returns non-list JSON → 200 with empty events
  - content-range total parsing (valid + malformed)
  - auth guard: 401 when no token
  - limit/offset defaults and query params passed
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
    """TestClient with auth override."""
    app.dependency_overrides[get_current_user] = lambda: TokenData(sub="test_user")
    return TestClient(app)


@pytest.fixture()
def unauth_client(app):
    """TestClient without auth override."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(rows: list, status: int = 200, content_range: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = rows
    resp.headers = {"content-range": content_range}
    resp.text = json.dumps(rows)
    return resp


def _patch_httpx(rows, status=200, content_range=""):
    mock_resp = _mock_response(rows, status, content_range)
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    return patch("routers.flow.httpx.AsyncClient", return_value=mock_client)


# _SAMPLE_ROW uses actual flow_events DB column names.
# - 'influence_tier'  (not 'tier'  — that was the old name)
# - 'created_at'      (not 'timestamp' — that column does not exist in DB)
f_SAMPLE_ROW = {
    "id": "abc123",
    "ticker": "AAPL",
    "strike": 180.0,
    "expiry": "2026-05-16",
    "dte": 17,
    "contract_type": "CALL",
    "trade_type": "SWEEP",
    "sentiment": "BULLISH",
    "premium": 15000.0,
    "size": 100,
    "bid": 1.45,
    "ask": 1.50,
    "fill_price": 1.47,
    "influence_tier": "T1",
    "conviction_score": 0.88,
    "is_aggressive": True,
    "is_golden_sweep": False,
    "iv": 0.42,
    "underlying_price": 178.50,
    "occ_symbol": "AAPL260516C00180000",
    "created_at": "2026-04-28T15:00:00Z",
}

_SAMPLE_ROW = f_SAMPLE_ROW


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_events_happy_path(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_SAMPLE_ROW], content_range="0-0/1"):
        resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["events"]) == 1
    e = data["events"][0]
    assert e["ticker"] == "AAPL"
    assert e["strike"] == 180.0
    assert e["contract_type"] == "CALL"
    assert e["trade_type"] == "SWEEP"
    assert e["sentiment"] == "BULLISH"
    assert e["premium"] == 15000.0
    assert e["size"] == 100
    assert e["bid"] == 1.45
    assert e["ask"] == 1.50
    assert e["fill_price"] == 1.47
    assert e["influence_tier"] == "T1"   # renamed from 'tier'
    assert e["conviction_score"] == 0.88
    assert e["is_aggressive"] is True
    assert e["is_golden_sweep"] is False
    assert e["iv"] == 0.42
    assert e["underlying_price"] == 178.50
    assert e["occ_symbol"] == "AAPL260516C00180000"
    assert e["timestamp"] == "2026-04-28T15:00:00Z"  # mapped from created_at


def test_events_default_limit_is_50(auth_client, monkeypatch):
    """Verify default limit=50 is in filters."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get("/api/flow/events")
    mock_client = mock_cls.return_value.__aenter__.return_value
    call_kwargs = mock_client.get.call_args
    params = call_kwargs[1]["params"]
    assert params["limit"] == "50"
    assert params["offset"] == "0"


# ---------------------------------------------------------------------------
# Ticker filter
# ---------------------------------------------------------------------------

def test_events_ticker_filter_uppercased(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_SAMPLE_ROW]) as mock_cls:
        resp = auth_client.get("/api/flow/events?ticker=aapl")
    assert resp.status_code == 200
    mock_client = mock_cls.return_value.__aenter__.return_value
    params = mock_client.get.call_args[1]["params"]
    assert params["ticker"] == "eq.AAPL"
    data = resp.json()
    assert data["filters"]["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# Filter passthrough
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("param,value,expected_key,expected_val", [
    ("sentiment",     "bullish",  "sentiment",      "eq.BULLISH"),
    ("contract_type", "put",      "contract_type",  "eq.PUT"),
    # URL query param is '?tier=', but the Supabase filter key is 'influence_tier'
    ("tier",          "t2",       "influence_tier",  "eq.T2"),
])
def test_events_string_filters(auth_client, monkeypatch, param, value, expected_key, expected_val):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get(f"/api/flow/events?{param}={value}")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params[expected_key] == expected_val


def test_events_aggressive_true(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get("/api/flow/events?aggressive=true")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params["is_aggressive"] == "eq.true"


def test_events_aggressive_false(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get("/api/flow/events?aggressive=false")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params["is_aggressive"] == "eq.false"


def test_events_golden_sweep_true(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get("/api/flow/events?golden_sweep=true")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params["is_golden_sweep"] == "eq.true"


def test_events_combined_filters_in_response(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]):
        resp = auth_client.get("/api/flow/events?ticker=SPY&sentiment=BEARISH&contract_type=PUT&tier=T1&aggressive=true&golden_sweep=false")
    data = resp.json()
    f = data["filters"]
    assert f["ticker"] == "SPY"
    assert f["sentiment"] == "BEARISH"
    assert f["contract_type"] == "PUT"
    assert f["tier"] == "T1"
    assert f["aggressive"] is True
    assert f["golden_sweep"] is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_events_null_optional_fields_handled(auth_client, monkeypatch):
    """Row with null bid/ask/fill_price/expiry/influence_tier returns without error."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    sparse_row = {
        "id": "x1", "ticker": "TSLA", "strike": None, "expiry": None,
        "dte": None, "contract_type": "PUT", "trade_type": None,
        "sentiment": "BEARISH", "premium": 5000.0, "size": 50,
        "bid": None, "ask": None, "fill_price": None,
        "influence_tier": None, "conviction_score": None,
        "is_aggressive": False, "is_golden_sweep": False,
        "iv": None, "underlying_price": None, "occ_symbol": None,
        "created_at": None,
    }
    with _patch_httpx([sparse_row]):
        resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    e = resp.json()["events"][0]
    assert e["bid"] is None
    assert e["ask"] is None
    assert e["fill_price"] is None
    assert e["influence_tier"] is None   # renamed from 'tier'
    assert e["timestamp"] is None


def test_events_row_parse_error_skipped(auth_client, monkeypatch):
    """A row that throws during FlowEventRaw construction is skipped."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    bad_row = {"premium": "not-a-float", "size": "bad"}  # missing required fields
    good_row = dict(_SAMPLE_ROW)
    with _patch_httpx([bad_row, good_row]):
        resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 1


def test_events_content_range_malformed(auth_client, monkeypatch):
    """If content-range cannot be parsed, fall back to len(rows)."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_SAMPLE_ROW], content_range="0-0/bad"):
        resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1  # fallback to len(rows)


def test_events_no_content_range_header(auth_client, monkeypatch):
    """Missing content-range header falls back to len(rows)."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([_SAMPLE_ROW], content_range=""):
        resp = auth_client.get("/api/flow/events")
    assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# Supabase env / error handling
# ---------------------------------------------------------------------------

def test_events_missing_supabase_url(auth_client):
    """Missing SUPABASE_URL → empty response, no crash."""
    resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []
    assert data["total"] == 0


def test_events_missing_supabase_key(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    assert resp.json()["events"] == []


def test_events_supabase_500(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([], status=500):
        resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    assert resp.json()["events"] == []


def test_events_supabase_non_list_json(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"error": "oops"}
    mock_resp.headers = {"content-range": ""}
    mock_resp.text = "{}"
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    with patch("routers.flow.httpx.AsyncClient", return_value=mock_client):
        resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    assert resp.json()["events"] == []


def test_events_httpx_exception(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("network error"))
    with patch("routers.flow.httpx.AsyncClient", return_value=mock_client):
        resp = auth_client.get("/api/flow/events")
    assert resp.status_code == 200
    assert resp.json()["events"] == []


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_events_requires_auth(unauth_client):
    resp = unauth_client.get("/api/flow/events")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_events_custom_limit_offset(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    with _patch_httpx([]) as mock_cls:
        auth_client.get("/api/flow/events?limit=25&offset=100")
    params = mock_cls.return_value.__aenter__.return_value.get.call_args[1]["params"]
    assert params["limit"] == "25"
    assert params["offset"] == "100"


def test_events_multiple_rows(auth_client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    row2 = dict(_SAMPLE_ROW, id="xyz", ticker="TSLA", premium=8000.0)
    with _patch_httpx([_SAMPLE_ROW, row2], content_range="0-1/50"):
        resp = auth_client.get("/api/flow/events")
    data = resp.json()
    assert data["total"] == 50
    assert len(data["events"]) == 2
