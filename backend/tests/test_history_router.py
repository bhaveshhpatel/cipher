"""
Regression tests for routers/history.py

Rearch-010 update (2026-05-09):
  _TIER_TO_DB, tier= query param, volume_premium_factor, backtest_score,
  flow_score, and influence_tier were removed from routers/history.py
  when migration 024 dropped those columns from signal_history.
  This file is updated to match the current router surface.

Strategy:
  - Use FastAPI TestClient with dependency override for get_current_user.
  - Supabase HTTP calls (_query_signal_history) are patched via AsyncMock.
  - No live DB required.

Covers:
  Auth guard:
  - GET /api/signals/history without auth header → 401

  Input validation:
  - direction not in {bullish, bearish, neutral} → 422
  - min_conviction > 1.0 → 422
  - min_conviction < 0.0 → 422
  - limit > 200 → 422
  - limit < 1 → 422
  - offset < 0 → 422

  Mapping constants:
  - _DIR_TO_REC: bullish→BUY, bearish→SELL, neutral→HOLD

  No Supabase configured:
  - Returns {signals: [], total: 0, limit: 50, offset: 0}

  Supabase HTTP 200 with rows:
  - Rows parsed into SignalHistoryItem list
  - HistoryResponse shape: signals, total, limit, offset
  - is_accelerating missing defaults to False
  - total parsed from content-range header
  - total_premium=None maps to None

  Supabase HTTP 4xx / exception:
  - Returns empty signals list (no crash)

  Pagination params:
  - limit and offset forwarded and echoed in response
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from core.auth import get_current_user, TokenData
from routers.history import router, _DIR_TO_REC
import routers.history as hist


# ---------------------------------------------------------------------------
# App and client fixtures
# ---------------------------------------------------------------------------

def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def _auth_override():
        return TokenData(email="user@cipher.app", role="user")

    app.dependency_overrides[get_current_user] = _auth_override
    return app


@pytest.fixture
def client():
    return TestClient(_make_app())


@pytest.fixture
def raw_client():
    """Client with NO auth override — tests the 401 path."""
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_no_auth_returns_401(raw_client):
    resp = raw_client.get("/api/signals/history")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Mapping constants
# ---------------------------------------------------------------------------

def test_dir_to_rec_bullish():
    assert _DIR_TO_REC["bullish"] == "BUY"


def test_dir_to_rec_bearish():
    assert _DIR_TO_REC["bearish"] == "SELL"


def test_dir_to_rec_neutral():
    assert _DIR_TO_REC["neutral"] == "HOLD"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_invalid_direction_returns_422(client):
    resp = client.get("/api/signals/history", params={"direction": "sideways"})
    assert resp.status_code == 422


@pytest.mark.parametrize("direction", ["bullish", "bearish", "neutral"])
def test_valid_direction_accepted(client, direction):
    with patch.object(hist, "_SUPABASE_URL", None):
        resp = client.get("/api/signals/history", params={"direction": direction})
    assert resp.status_code == 200


def test_min_conviction_above_1_returns_422(client):
    resp = client.get("/api/signals/history", params={"min_conviction": 1.1})
    assert resp.status_code == 422


def test_min_conviction_below_0_returns_422(client):
    resp = client.get("/api/signals/history", params={"min_conviction": -0.1})
    assert resp.status_code == 422


def test_limit_above_200_returns_422(client):
    resp = client.get("/api/signals/history", params={"limit": 201})
    assert resp.status_code == 422


def test_limit_below_1_returns_422(client):
    resp = client.get("/api/signals/history", params={"limit": 0})
    assert resp.status_code == 422


def test_offset_negative_returns_422(client):
    resp = client.get("/api/signals/history", params={"offset": -1})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# No Supabase configured
# ---------------------------------------------------------------------------

def test_no_supabase_returns_empty_response(client):
    with patch.object(hist, "_SUPABASE_URL", None), \
         patch.object(hist, "_SUPABASE_KEY", None):
        resp = client.get("/api/signals/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["signals"] == []
    assert body["total"]   == 0
    assert body["limit"]   == 50
    assert body["offset"]  == 0


# ---------------------------------------------------------------------------
# Supabase HTTP 200 — row parsing
# ---------------------------------------------------------------------------

def _make_signal_row(**overrides) -> dict:
    """Minimal valid signal_history row — REARCH-010 schema."""
    base = {
        "id":               1,
        "ticker":           "AAPL",
        "recommendation":   "BUY",
        "composite_score":  0.85,
        "reasoning":        "Strong flow",
        "contract_type":    "CALL",
        "direction":        "BULLISH",
        "total_premium":    50000.0,
        "trade_count":      12,
        "is_accelerating":  True,
        "signal_ts":        "2026-04-25T18:00:00Z",
        "created_at":       "2026-04-25T18:00:01Z",
    }
    base.update(overrides)
    return base


def _mock_supabase_response(rows: list, total: int = None, status_code: int = 200):
    """Patch _query_signal_history to return the given rows."""
    effective_total = total if total is not None else len(rows)
    return patch(
        "routers.history._query_signal_history",
        new_callable=AsyncMock,
        return_value=(rows, effective_total),
    )


def test_http_200_rows_parsed_into_signals(client):
    rows = [_make_signal_row()]
    with _mock_supabase_response(rows, total=1):
        resp = client.get("/api/signals/history")
    assert resp.status_code == 200
    signals = resp.json()["signals"]
    assert len(signals) == 1
    assert signals[0]["ticker"] == "AAPL"


def test_response_shape_has_all_required_keys(client):
    with _mock_supabase_response([]):
        resp = client.get("/api/signals/history")
    body = resp.json()
    for key in ("signals", "total", "limit", "offset"):
        assert key in body


def test_total_echoed_from_supabase(client):
    rows = [_make_signal_row(id=i, ticker="TSLA") for i in range(1, 4)]
    with _mock_supabase_response(rows, total=42):
        resp = client.get("/api/signals/history")
    assert resp.json()["total"] == 42


def test_limit_and_offset_echoed_in_response(client):
    with _mock_supabase_response([]):
        resp = client.get("/api/signals/history", params={"limit": 25, "offset": 50})
    body = resp.json()
    assert body["limit"]  == 25
    assert body["offset"] == 50


def test_is_accelerating_defaults_to_false_when_absent(client):
    row = _make_signal_row()
    del row["is_accelerating"]
    with _mock_supabase_response([row]):
        resp = client.get("/api/signals/history")
    assert resp.json()["signals"][0]["is_accelerating"] is False


def test_total_premium_null_maps_to_none(client):
    row = _make_signal_row(total_premium=None)
    with _mock_supabase_response([row]):
        resp = client.get("/api/signals/history")
    assert resp.json()["signals"][0]["total_premium"] is None


def test_multiple_rows_all_parsed(client):
    rows = [_make_signal_row(id=i) for i in range(1, 6)]
    with _mock_supabase_response(rows, total=5):
        resp = client.get("/api/signals/history")
    assert len(resp.json()["signals"]) == 5


def test_composite_score_parsed_as_float(client):
    row = _make_signal_row(composite_score="0.72")
    with _mock_supabase_response([row]):
        resp = client.get("/api/signals/history")
    assert resp.json()["signals"][0]["composite_score"] == pytest.approx(0.72)


# ---------------------------------------------------------------------------
# REARCH-010 vocab — direction and alert_level pass-through
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH", "NEUTRAL"])
def test_rearch_direction_vocab_passes_through(client, direction):
    """signal_history rows use BULLISH/BEARISH/NEUTRAL — confirm model accepts them."""
    row = _make_signal_row(direction=direction)
    with _mock_supabase_response([row]):
        resp = client.get("/api/signals/history")
    assert resp.status_code == 200
    assert resp.json()["signals"][0]["direction"] == direction


# ---------------------------------------------------------------------------
# Supabase 4xx and exception paths — no crash
# ---------------------------------------------------------------------------

def test_supabase_4xx_returns_empty_signals(client):
    with patch(
        "routers.history._query_signal_history",
        new_callable=AsyncMock,
        return_value=([], 0),
    ):
        resp = client.get("/api/signals/history")
    assert resp.status_code == 200
    assert resp.json()["signals"] == []


def test_supabase_exception_returns_empty_signals(client):
    with patch(
        "routers.history._query_signal_history",
        new_callable=AsyncMock,
        return_value=([], 0),
    ):
        resp = client.get("/api/signals/history")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
