"""
Regression tests for routers/history.py

Covers:
  - Unauthenticated request returns 401
  - Basic authenticated request returns HistoryResponse shape
  - direction validation (invalid value → 422)
  - tier validation (invalid value → 422)
  - min_conviction out-of-range validation (> 1.0 → 422)
  - All valid direction values are accepted
  - All valid tier values are accepted
  - Empty result set (no Supabase env vars configured)
  - Pagination params (limit + offset) are forwarded
  - ticker filter uppercase normalisation
  - Supabase unavailable (env vars missing) returns empty list, not 500
"""
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from core.auth import create_access_token, TokenData
from main import app

client = TestClient(app)


def _make_token(email="trader@cipher.io", role="user") -> str:
    return create_access_token({"sub": email})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_user(role="user"):
    td = TokenData(email="trader@cipher.io", role=role)
    return patch("routers.history.get_current_user", return_value=td)


def _mock_query(rows=None, total=0):
    return patch(
        "routers.history._query_signal_history",
        new=AsyncMock(return_value=(rows or [], total)),
    )


_SAMPLE_ROW = {
    "id": 1,
    "ticker": "AAPL",
    "recommendation": "BUY",
    "composite_score": 0.85,
    "flow_score": 0.80,
    "backtest_score": 0.75,
    "volume_premium_factor": 0.6,
    "reasoning": "Strong call sweep",
    "contract_type": "CALL",
    "direction": "bullish",
    "influence_tier": "WHALE",
    "total_premium": 1_200_000.0,
    "trade_count": 14,
    "is_accelerating": True,
    "signal_ts": "2026-04-25T18:00:00Z",
    "created_at": "2026-04-25T18:00:01Z",
}


def test_history_unauthenticated_returns_401():
    resp = client.get("/api/signals/history")
    assert resp.status_code == 401


def test_history_returns_valid_response_shape():
    with _mock_user(), _mock_query([_SAMPLE_ROW], total=1):
        resp = client.get(
            "/api/signals/history",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "signals" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert body["total"] == 1
    assert len(body["signals"]) == 1
    sig = body["signals"][0]
    assert sig["ticker"] == "AAPL"
    assert sig["recommendation"] == "BUY"
    assert sig["composite_score"] == pytest.approx(0.85)


def test_history_empty_result_returns_zero_list():
    with _mock_user(), _mock_query([], total=0):
        resp = client.get(
            "/api/signals/history",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["signals"] == []
    assert body["total"] == 0


def test_history_invalid_direction_returns_422():
    with _mock_user(), _mock_query():
        resp = client.get(
            "/api/signals/history?direction=long",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 422
    assert "direction" in resp.json()["detail"].lower()


def test_history_invalid_tier_returns_422():
    with _mock_user(), _mock_query():
        resp = client.get(
            "/api/signals/history?tier=bigfish",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 422
    assert "tier" in resp.json()["detail"].lower()


def test_history_min_conviction_above_1_returns_422():
    with _mock_user(), _mock_query():
        resp = client.get(
            "/api/signals/history?min_conviction=1.5",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 422


def test_history_min_conviction_below_0_returns_422():
    with _mock_user(), _mock_query():
        resp = client.get(
            "/api/signals/history?min_conviction=-0.1",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 422


@pytest.mark.parametrize("direction", ["bullish", "bearish", "neutral"])
def test_history_valid_directions_accepted(direction):
    with _mock_user(), _mock_query():
        resp = client.get(
            f"/api/signals/history?direction={direction}",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 200


@pytest.mark.parametrize("tier", ["whale", "institutional", "large", "retail"])
def test_history_valid_tiers_accepted(tier):
    with _mock_user(), _mock_query():
        resp = client.get(
            f"/api/signals/history?tier={tier}",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 200


def test_history_direction_bullish_maps_to_BUY():
    captured = {}

    async def capture_query(**kwargs):
        captured.update(kwargs)
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", new=AsyncMock(side_effect=capture_query)):
            client.get(
                "/api/signals/history?direction=bullish",
                headers=_auth(_make_token()),
            )
    assert captured.get("recommendation") == "BUY"


def test_history_direction_bearish_maps_to_SELL():
    captured = {}

    async def capture_query(**kwargs):
        captured.update(kwargs)
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", new=AsyncMock(side_effect=capture_query)):
            client.get(
                "/api/signals/history?direction=bearish",
                headers=_auth(_make_token()),
            )
    assert captured.get("recommendation") == "SELL"


def test_history_ticker_is_uppercased():
    captured = {}

    async def capture_query(**kwargs):
        captured.update(kwargs)
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", new=AsyncMock(side_effect=capture_query)):
            client.get(
                "/api/signals/history?ticker=aapl",
                headers=_auth(_make_token()),
            )
    assert captured.get("ticker") == "AAPL"


def test_history_pagination_params_forwarded():
    captured = {}

    async def capture_query(**kwargs):
        captured.update(kwargs)
        return [], 0

    with _mock_user():
        with patch("routers.history._query_signal_history", new=AsyncMock(side_effect=capture_query)):
            resp = client.get(
                "/api/signals/history?limit=25&offset=50",
                headers=_auth(_make_token()),
            )
    assert resp.status_code == 200
    assert captured["limit"] == 25
    assert captured["offset"] == 50


def test_history_response_echoes_limit_and_offset():
    with _mock_user(), _mock_query([], total=0):
        resp = client.get(
            "/api/signals/history?limit=10&offset=20",
            headers=_auth(_make_token()),
        )
    body = resp.json()
    assert body["limit"] == 10
    assert body["offset"] == 20


def test_history_no_supabase_env_returns_empty_not_500():
    with _mock_user():
        with patch("routers.history._SUPABASE_URL", None), \
             patch("routers.history._SUPABASE_KEY", None):
            resp = client.get(
                "/api/signals/history",
                headers=_auth(_make_token()),
            )
    assert resp.status_code == 200
    assert resp.json()["signals"] == []
    assert resp.json()["total"] == 0


def test_history_malformed_row_is_skipped():
    bad_row = {"id": 2, "ticker": "SPY"}
    good_row = _SAMPLE_ROW.copy()
    with _mock_user(), _mock_query([bad_row, good_row], total=2):
        resp = client.get(
            "/api/signals/history",
            headers=_auth(_make_token()),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["signals"]) == 1
    assert body["signals"][0]["ticker"] == "AAPL"
