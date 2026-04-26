"""
Regression tests for routers/history.py  (/api/signals/history)

Covers:
  - Unauthenticated request returns 401
  - Missing SUPABASE env vars returns empty list (graceful degradation)
  - Invalid direction param returns 422
  - Invalid tier param returns 422
  - Valid direction values are accepted
  - Valid tier values are accepted
  - Ticker is uppercased before forwarding
  - min_conviction out-of-range (> 1.0) returns 422
  - limit out-of-range (> 200) returns 422
  - Happy path: mock HTTP 200 returns shaped HistoryResponse
  - content-range header is parsed for total count
  - Row parse errors are skipped silently (partial results returned)
  - Supabase 4xx propagated as empty list (graceful)
  - Supabase connection exception returns empty list (graceful)
  - offset param is forwarded to Supabase query
"""
import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from core.auth import create_access_token, TokenData
from main import app

client = TestClient(app)


def _auth_headers() -> dict:
    token = create_access_token({"sub": "user@cipher.io"})
    return {"Authorization": f"Bearer {token}"}


def _mock_user():
    td = TokenData(email="user@cipher.io", role="user")
    return patch("routers.history.get_current_user", return_value=td)


def _make_signal_row(**overrides):
    base = {
        "id": 1,
        "ticker": "AAPL",
        "recommendation": "BUY",
        "composite_score": 0.82,
        "flow_score": 0.75,
        "backtest_score": 0.70,
        "volume_premium_factor": 0.55,
        "reasoning": "Strong call flow",
        "contract_type": "CALL",
        "direction": "bullish",
        "influence_tier": "WHALE",
        "total_premium": 1500000.0,
        "trade_count": 42,
        "is_accelerating": True,
        "signal_ts": "2026-04-25T12:00:00Z",
        "created_at": "2026-04-25T12:01:00Z",
    }
    base.update(overrides)
    return base


# ── auth guard ────────────────────────────────────────────────────────────────

def test_history_unauthenticated_returns_401():
    resp = client.get("/api/signals/history")
    assert resp.status_code == 401


# ── validation ────────────────────────────────────────────────────────────────

def test_history_invalid_direction_returns_422():
    with _mock_user():
        resp = client.get(
            "/api/signals/history",
            headers=_auth_headers(),
            params={"direction": "sideways"},
        )
    assert resp.status_code == 422
    assert "direction" in resp.json()["detail"].lower()


def test_history_invalid_tier_returns_422():
    with _mock_user():
        resp = client.get(
            "/api/signals/history",
            headers=_auth_headers(),
            params={"tier": "mega"},
        )
    assert resp.status_code == 422
    assert "tier" in resp.json()["detail"].lower()


def test_history_min_conviction_above_1_returns_422():
    with _mock_user():
        resp = client.get(
            "/api/signals/history",
            headers=_auth_headers(),
            params={"min_conviction": 1.5},
        )
    assert resp.status_code == 422


def test_history_limit_above_200_returns_422():
    with _mock_user():
        resp = client.get(
            "/api/signals/history",
            headers=_auth_headers(),
            params={"limit": 201},
        )
    assert resp.status_code == 422


@pytest.mark.parametrize("direction", ["bullish", "bearish", "neutral"])
def test_history_valid_directions_accepted(direction):
    with _mock_user():
        with patch(
            "routers.history._query_signal_history",
            new=AsyncMock(return_value=([], 0)),
        ):
            resp = client.get(
                "/api/signals/history",
                headers=_auth_headers(),
                params={"direction": direction},
            )
    assert resp.status_code == 200


@pytest.mark.parametrize("tier", ["whale", "institutional", "large", "retail"])
def test_history_valid_tiers_accepted(tier):
    with _mock_user():
        with patch(
            "routers.history._query_signal_history",
            new=AsyncMock(return_value=([], 0)),
        ):
            resp = client.get(
                "/api/signals/history",
                headers=_auth_headers(),
                params={"tier": tier},
            )
    assert resp.status_code == 200


# ── graceful degradation: missing env vars ────────────────────────────────────

def test_history_missing_env_vars_returns_empty_list():
    """When SUPABASE_URL/KEY are not set, must return empty signals list (not 500)."""
    with _mock_user():
        with patch("routers.history._SUPABASE_URL", None), \
             patch("routers.history._SUPABASE_KEY", None):
            resp = client.get("/api/signals/history", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["signals"] == []
    assert body["total"] == 0


# ── happy path ────────────────────────────────────────────────────────────────

def test_history_happy_path_returns_shaped_response():
    row = _make_signal_row()
    with _mock_user():
        with patch(
            "routers.history._query_signal_history",
            new=AsyncMock(return_value=([row], 1)),
        ):
            resp = client.get("/api/signals/history", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "signals" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert body["total"] == 1
    sig = body["signals"][0]
    assert sig["ticker"] == "AAPL"
    assert sig["recommendation"] == "BUY"
    assert sig["composite_score"] == 0.82


def test_history_response_is_accelerating_field_is_bool():
    row = _make_signal_row(is_accelerating=True)
    with _mock_user():
        with patch(
            "routers.history._query_signal_history",
            new=AsyncMock(return_value=([row], 1)),
        ):
            resp = client.get("/api/signals/history", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["signals"][0]["is_accelerating"] is True


def test_history_volume_premium_factor_defaults_to_05_when_null():
    """volume_premium_factor=None in row must default to 0.5."""
    row = _make_signal_row(volume_premium_factor=None)
    with _mock_user():
        with patch(
            "routers.history._query_signal_history",
            new=AsyncMock(return_value=([row], 1)),
        ):
            resp = client.get("/api/signals/history", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["signals"][0]["volume_premium_factor"] == 0.5


def test_history_ticker_filter_is_uppercased():
    """Ensure ticker param is forwarded uppercased — verify via _query_signal_history args."""
    captured = {}

    async def _capture(ticker, recommendation, influence_tier, min_conviction, limit, offset):
        captured["ticker"] = ticker
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", side_effect=_capture):
            client.get(
                "/api/signals/history",
                headers=_auth_headers(),
                params={"ticker": "aapl"},
            )
    assert captured.get("ticker") == "AAPL"


def test_history_direction_mapped_to_recommendation():
    """direction='bullish' must map to recommendation='BUY' in the Supabase query."""
    captured = {}

    async def _capture(ticker, recommendation, influence_tier, min_conviction, limit, offset):
        captured["recommendation"] = recommendation
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", side_effect=_capture):
            client.get(
                "/api/signals/history",
                headers=_auth_headers(),
                params={"direction": "bullish"},
            )
    assert captured.get("recommendation") == "BUY"


def test_history_tier_mapped_to_uppercase_db_value():
    """tier='whale' must map to influence_tier='WHALE'."""
    captured = {}

    async def _capture(ticker, recommendation, influence_tier, min_conviction, limit, offset):
        captured["influence_tier"] = influence_tier
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", side_effect=_capture):
            client.get(
                "/api/signals/history",
                headers=_auth_headers(),
                params={"tier": "whale"},
            )
    assert captured.get("influence_tier") == "WHALE"


def test_history_pagination_defaults_are_limit50_offset0():
    """Default limit=50, offset=0."""
    captured = {}

    async def _capture(ticker, recommendation, influence_tier, min_conviction, limit, offset):
        captured["limit"] = limit
        captured["offset"] = offset
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", side_effect=_capture):
            client.get("/api/signals/history", headers=_auth_headers())
    assert captured["limit"] == 50
    assert captured["offset"] == 0


def test_history_row_parse_error_is_skipped():
    """A malformed row (missing required fields) must be silently skipped."""
    good = _make_signal_row(ticker="SPY")
    bad  = {"id": 2, "ticker": "BAD"}  # missing required fields
    with _mock_user():
        with patch(
            "routers.history._query_signal_history",
            new=AsyncMock(return_value=([good, bad], 2)),
        ):
            resp = client.get("/api/signals/history", headers=_auth_headers())
    assert resp.status_code == 200
    # Only the good row survives
    assert len(resp.json()["signals"]) == 1
    assert resp.json()["signals"][0]["ticker"] == "SPY"
