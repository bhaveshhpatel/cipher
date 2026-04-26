"""
Phase 4 — test_smart_signals_router.py

Covers every branch in routers/smart_signals.py:
  - GET /api/signals/composite/{ticker}: DB hit path → returns DB row
  - GET /api/signals/composite/{ticker}: DB miss path → returns mock fallback
  - GET /api/signals/composite/{ticker}: unauthenticated → 401/403
  - GET /api/signals/list: DB hit → source='live', pagination, total from content-range
  - GET /api/signals/list: DB empty → source='mock', returns mock tickers
  - GET /api/signals/list: direction filter applied to mock fallback
  - GET /api/signals/list: min_conviction filter applied to mock fallback
  - GET /api/signals/list: invalid direction → 422
  - GET /api/signals/list: invalid tier → 422
  - GET /api/signals/list: unauthenticated → 401/403
  - GET /api/signals/stream/stats: success path
  - _mock_composite(): deterministic for same ticker, in valid range
  - _row_to_composite(): missing volume_premium_factor defaults to 0.5
  - _row_to_composite(): missing reasoning defaults to ''
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def _make_app():
    from routers.smart_signals import router
    from core.auth import get_current_user, TokenData
    app = FastAPI()
    app.include_router(router)
    fake_user = TokenData(user_id="test-uid", email="test@example.com")
    async def _fake_auth():
        return fake_user
    app.dependency_overrides[get_current_user] = _fake_auth
    return app


_DB_ROW = {
    "ticker":                "AAPL",
    "recommendation":        "BUY",
    "composite_score":       0.72,
    "flow_score":            0.81,
    "backtest_score":        0.65,
    "volume_premium_factor": 0.55,
    "reasoning":             "Whale activity detected.",
}


class TestCompositeEndpoint:

    def test_db_hit_returns_db_row(self):
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_ticker_from_db", AsyncMock(return_value=_DB_ROW)):
            resp = client.get("/api/signals/composite/AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"]          == "AAPL"
        assert body["recommendation"]  == "BUY"
        assert body["composite_score"] == 0.72

    def test_db_miss_returns_mock(self):
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_ticker_from_db", AsyncMock(return_value=None)):
            resp = client.get("/api/signals/composite/TSLA")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "TSLA"
        assert body["recommendation"] in {"BUY", "SELL", "HOLD"}
        assert 0.0 <= body["composite_score"] <= 1.0

    def test_mock_is_deterministic(self):
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_ticker_from_db", AsyncMock(return_value=None)):
            r1 = client.get("/api/signals/composite/NVDA").json()
            r2 = client.get("/api/signals/composite/NVDA").json()
        assert r1["composite_score"] == r2["composite_score"]

    def test_ticker_uppercased_in_mock(self):
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_ticker_from_db", AsyncMock(return_value=None)):
            resp = client.get("/api/signals/composite/spy")
        assert resp.json()["ticker"] == "SPY"

    def test_unauthenticated_blocked(self):
        from routers.smart_signals import router
        bare_app = FastAPI()
        bare_app.include_router(router)
        client = TestClient(bare_app, raise_server_exceptions=False)
        resp = client.get("/api/signals/composite/AAPL")
        assert resp.status_code in (401, 403)


class TestListEndpoint:

    def test_db_hit_returns_live_source(self):
        rows = [_DB_ROW.copy() for _ in range(3)]
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_from_db", AsyncMock(return_value=(rows, 3))):
            resp = client.get("/api/signals/list")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"]  == "live"
        assert body["total"]   == 3
        assert len(body["signals"]) == 3

    def test_db_empty_returns_mock_source(self):
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_from_db", AsyncMock(return_value=([], 0))):
            resp = client.get("/api/signals/list")
        assert resp.status_code == 200
        assert resp.json()["source"] == "mock"
        assert resp.json()["total"] == 20  # full mock dataset

    def test_direction_filter_applied_to_mock(self):
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_from_db", AsyncMock(return_value=([], 0))):
            resp = client.get("/api/signals/list?direction=bullish")
        body = resp.json()
        for sig in body["signals"]:
            assert sig["recommendation"] == "BUY"

    def test_invalid_direction_returns_422(self):
        app    = _make_app()
        client = TestClient(app)
        resp = client.get("/api/signals/list?direction=maybe")
        assert resp.status_code == 422

    def test_invalid_tier_returns_422(self):
        app    = _make_app()
        client = TestClient(app)
        resp = client.get("/api/signals/list?tier=mega")
        assert resp.status_code == 422

    def test_min_conviction_filter_applied_to_mock(self):
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_from_db", AsyncMock(return_value=([], 0))):
            resp = client.get("/api/signals/list?min_conviction=0.7")
        for sig in resp.json()["signals"]:
            assert sig["composite_score"] >= 0.7

    def test_pagination_page_and_page_size(self):
        rows = [_DB_ROW.copy() for _ in range(10)]
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_from_db", AsyncMock(return_value=(rows, 50))):
            resp = client.get("/api/signals/list?page=2&page_size=10")
        body = resp.json()
        assert body["page"]      == 2
        assert body["page_size"] == 10
        assert body["total"]     == 50

    def test_unauthenticated_blocked(self):
        from routers.smart_signals import router
        bare_app = FastAPI()
        bare_app.include_router(router)
        client = TestClient(bare_app, raise_server_exceptions=False)
        resp = client.get("/api/signals/list")
        assert resp.status_code in (401, 403)

    def test_live_row_parse_error_skipped(self):
        """Rows with missing required fields are skipped, not crash the endpoint."""
        bad_row = {"ticker": "AAPL"}  # missing composite_score etc.
        app    = _make_app()
        client = TestClient(app)
        with patch("routers.smart_signals._fetch_from_db",
                   AsyncMock(return_value=([_DB_ROW.copy(), bad_row], 2))):
            resp = client.get("/api/signals/list")
        # only the good row survives
        assert resp.status_code == 200
        assert len(resp.json()["signals"]) == 1


class TestStreamStatsEndpoint:

    def test_stream_stats_success(self):
        app    = _make_app()
        client = TestClient(app)
        mock_stats = {"active_symbols": 150, "ticks": 1000,
                      "classified": 800, "signals": 45, "errors": 2}
        with patch("routers.smart_signals.get_stats", return_value=mock_stats):
            resp = client.get("/api/signals/stream/stats")
        assert resp.status_code == 200
        body = resp.json()["stats"]
        assert body["active_symbols"] == 150
        assert body["ticks"]          == 1000


class TestRowToComposite:

    def test_missing_volume_premium_factor_defaults_to_0_5(self):
        from routers.smart_signals import _row_to_composite
        row = {
            "ticker":          "AAPL",
            "recommendation":  "BUY",
            "composite_score": 0.7,
            "flow_score":      0.8,
            "backtest_score":  0.65,
            # no volume_premium_factor
            "reasoning":       "Strong.",
        }
        out = _row_to_composite(row)
        assert out.volume_premium_factor == 0.5

    def test_missing_reasoning_defaults_to_empty_string(self):
        from routers.smart_signals import _row_to_composite
        row = {
            "ticker":                "AAPL",
            "recommendation":        "HOLD",
            "composite_score":       0.5,
            "flow_score":            0.5,
            "backtest_score":        0.5,
            "volume_premium_factor": 0.5,
            # no reasoning
        }
        out = _row_to_composite(row)
        assert out.reasoning == ""
