"""
Regression tests for routers/smart_signals.py

Strategy:
  - Override get_current_user for all auth-passing tests.
  - _fetch_from_db and _fetch_ticker_from_db patched to avoid
    live HTTP calls to Supabase.
  - get_stats patched to return a controlled dict.
  - _mock_composite and _row_to_composite tested directly.

Covers:
  Auth guard:
  - GET /api/signals/composite/{ticker} -> 401 without auth
  - GET /api/signals/list -> 401 without auth
  - GET /api/signals/stream/stats -> 401 without auth

  GET /api/signals/composite/{ticker}:
  - DB hit: row served via _row_to_composite
  - DB miss: mock_composite fallback used
  - Ticker uppercased before DB query
  - ticker 1 char accepted, >10 chars -> 422
  - CompositeOut shape: all required fields present

  GET /api/signals/list:
  - Invalid direction -> 422
  - Valid directions accepted
  - tier param removed in rearch-010 (influence_tier col dropped)
  - min_conviction > 1.0 -> 422
  - min_conviction < 0.0 -> 422
  - page_size > 100 -> 422
  - page < 1 -> 422
  - DB empty -> mock fallback, source='mock'
  - DB returns rows -> source='live'
  - SignalsListResponse shape: signals/page/page_size/total/source
  - Mock dataset filtered by direction (rec_filter)
  - Mock dataset filtered by min_conviction

  GET /api/signals/stream/stats:
  - Response has 'stats' key with all 5 fields

  Internal helpers:
  - _mock_composite: deterministic (same ticker, same result)
  - _row_to_composite: null reasoning -> empty string
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from core.auth import get_current_user, TokenData
from routers.smart_signals import (
    router,
    _mock_composite,
    _row_to_composite,
)
import routers.smart_signals as ss


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------

def _make_app(authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if authenticated:
        async def _auth():
            return TokenData(email="user@cipher.app", role="user")
        app.dependency_overrides[get_current_user] = _auth
    return app


@pytest.fixture
def client():
    return TestClient(_make_app(authenticated=True))


@pytest.fixture
def raw_client():
    return TestClient(_make_app(authenticated=False), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_composite_no_auth_returns_401(raw_client):
    resp = raw_client.get("/api/signals/composite/AAPL")
    assert resp.status_code == 401


def test_list_no_auth_returns_401(raw_client):
    resp = raw_client.get("/api/signals/list")
    assert resp.status_code == 401


def test_stream_stats_no_auth_returns_401(raw_client):
    resp = raw_client.get("/api/signals/stream/stats")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/signals/composite/{ticker}
# ---------------------------------------------------------------------------

def test_composite_db_hit_returns_row(client):
    # rearch-010: only ticker/recommendation/composite_score/reasoning
    # remain in signal_history select and CompositeOut.
    db_row = {
        "ticker": "AAPL", "recommendation": "BUY",
        "composite_score": 0.85,
        "reasoning": "Strong call flow",
    }
    with patch.object(ss, "_fetch_ticker_from_db", new_callable=AsyncMock,
                      return_value=db_row):
        resp = client.get("/api/signals/composite/AAPL")
    assert resp.status_code == 200
    assert resp.json()["recommendation"] == "BUY"
    assert resp.json()["ticker"] == "AAPL"


def test_composite_db_miss_returns_mock(client):
    with patch.object(ss, "_fetch_ticker_from_db", new_callable=AsyncMock,
                      return_value=None):
        resp = client.get("/api/signals/composite/TSLA")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "TSLA"
    assert body["recommendation"] in ("BUY", "SELL", "HOLD")


def test_composite_ticker_uppercased(client):
    captured = []

    async def _capture(ticker):
        captured.append(ticker)
        return None

    with patch.object(ss, "_fetch_ticker_from_db", side_effect=_capture):
        client.get("/api/signals/composite/tsla")

    assert captured[0] == "TSLA"


def test_composite_single_char_ticker_accepted(client):
    with patch.object(ss, "_fetch_ticker_from_db", new_callable=AsyncMock, return_value=None):
        resp = client.get("/api/signals/composite/X")
    assert resp.status_code == 200


def test_composite_ticker_too_long_returns_422(client):
    resp = client.get("/api/signals/composite/TOOLONGTICKER")
    assert resp.status_code == 422


def test_composite_out_shape(client):
    """
    rearch-010: CompositeOut was trimmed to 4 fields.
    flow_score, backtest_score, volume_premium_factor were all dropped
    from signal_history in migration 024 and removed from the model.
    """
    with patch.object(ss, "_fetch_ticker_from_db", new_callable=AsyncMock, return_value=None):
        resp = client.get("/api/signals/composite/NVDA")
    body = resp.json()
    for key in ("ticker", "recommendation", "composite_score", "reasoning"):
        assert key in body
    # Confirm removed fields are absent
    for gone in ("flow_score", "backtest_score", "volume_premium_factor"):
        assert gone not in body


# ---------------------------------------------------------------------------
# GET /api/signals/list — validation
# ---------------------------------------------------------------------------

def test_list_invalid_direction_returns_422(client):
    resp = client.get("/api/signals/list", params={"direction": "sideways"})
    assert resp.status_code == 422


@pytest.mark.parametrize("direction", ["bullish", "bearish", "neutral"])
def test_list_valid_direction_accepted(client, direction):
    with patch.object(ss, "_fetch_from_db", new_callable=AsyncMock, return_value=([], 0)):
        resp = client.get("/api/signals/list", params={"direction": direction})
    assert resp.status_code == 200


def test_list_tier_param_removed_returns_200(client):
    """
    rearch-010: influence_tier column dropped in migration 024.
    The `tier` query param was removed from /list. Passing an unknown
    query param to FastAPI is silently ignored -> 200, not 422.
    """
    with patch.object(ss, "_fetch_from_db", new_callable=AsyncMock, return_value=([], 0)):
        resp = client.get("/api/signals/list", params={"tier": "hedge_fund"})
    assert resp.status_code == 200


def test_list_min_conviction_above_1_returns_422(client):
    resp = client.get("/api/signals/list", params={"min_conviction": 1.1})
    assert resp.status_code == 422


def test_list_min_conviction_below_0_returns_422(client):
    resp = client.get("/api/signals/list", params={"min_conviction": -0.1})
    assert resp.status_code == 422


def test_list_page_size_above_100_returns_422(client):
    resp = client.get("/api/signals/list", params={"page_size": 101})
    assert resp.status_code == 422


def test_list_page_below_1_returns_422(client):
    resp = client.get("/api/signals/list", params={"page": 0})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/signals/list — DB empty -> mock fallback
# ---------------------------------------------------------------------------

def test_list_db_empty_returns_mock_source(client):
    with patch.object(ss, "_fetch_from_db", new_callable=AsyncMock, return_value=([], 0)):
        resp = client.get("/api/signals/list")
    assert resp.status_code == 200
    assert resp.json()["source"] == "mock"


def test_list_db_rows_returns_live_source(client):
    rows = [{
        "ticker": "AAPL", "recommendation": "BUY",
        "composite_score": 0.85,
        "reasoning": "test",
    }]
    with patch.object(ss, "_fetch_from_db", new_callable=AsyncMock, return_value=(rows, 1)):
        resp = client.get("/api/signals/list")
    assert resp.json()["source"] == "live"


def test_list_response_shape(client):
    with patch.object(ss, "_fetch_from_db", new_callable=AsyncMock, return_value=([], 0)):
        resp = client.get("/api/signals/list")
    body = resp.json()
    for key in ("signals", "page", "page_size", "total", "source"):
        assert key in body


def test_list_mock_filtered_by_direction(client):
    """When DB is empty, mock dataset filters by rec_filter."""
    with patch.object(ss, "_fetch_from_db", new_callable=AsyncMock, return_value=([], 0)):
        resp = client.get("/api/signals/list", params={"direction": "bullish"})
    signals = resp.json()["signals"]
    for sig in signals:
        assert sig["recommendation"] == "BUY"


def test_list_mock_filtered_by_min_conviction(client):
    """When DB is empty, mock filters by min_conviction."""
    with patch.object(ss, "_fetch_from_db", new_callable=AsyncMock, return_value=([], 0)):
        resp = client.get("/api/signals/list", params={"min_conviction": 0.9})
    signals = resp.json()["signals"]
    for sig in signals:
        assert sig["composite_score"] >= 0.9


# ---------------------------------------------------------------------------
# GET /api/signals/stream/stats
# ---------------------------------------------------------------------------

def test_stream_stats_response_shape(client):
    mock_stats = {
        "active_symbols": 10, "ticks": 500,
        "classified": 480, "signals": 42, "errors": 2,
    }
    with patch("routers.smart_signals.get_stats", return_value=mock_stats):
        resp = client.get("/api/signals/stream/stats")
    assert resp.status_code == 200
    stats = resp.json()["stats"]
    for key in ("active_symbols", "ticks", "classified", "signals", "errors"):
        assert key in stats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def test_mock_composite_is_deterministic():
    """Same ticker always produces identical output."""
    r1 = _mock_composite("AAPL")
    r2 = _mock_composite("AAPL")
    assert r1.composite_score == r2.composite_score
    assert r1.recommendation  == r2.recommendation


def test_mock_composite_different_tickers_differ():
    r_aapl = _mock_composite("AAPL")
    r_tsla = _mock_composite("TSLA")
    assert r_aapl.ticker != r_tsla.ticker


def test_row_to_composite_null_reasoning_becomes_empty_string():
    """
    rearch-010: only ticker/recommendation/composite_score/reasoning
    remain. reasoning=None should coerce to empty string.
    """
    row = {
        "ticker": "Y", "recommendation": "HOLD",
        "composite_score": 0.5,
        "reasoning": None,
    }
    result = _row_to_composite(row)
    assert result.reasoning == ""


def test_row_to_composite_reasoning_passthrough():
    """Non-null reasoning is passed through unchanged."""
    row = {
        "ticker": "Z", "recommendation": "BUY",
        "composite_score": 0.8,
        "reasoning": "Strong sweep activity.",
    }
    result = _row_to_composite(row)
    assert result.reasoning == "Strong sweep activity."
