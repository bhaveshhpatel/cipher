"""
Tests for /api/flow/scan and /api/stream/stats endpoints.
Validates all-ticker fetch, ticker filter, pagination, and auth guard.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from main import app

client = TestClient(app)

# ── helpers ──────────────────────────────────────────────────────────────────

def register_and_login(email: str, password: str = "pw123456") -> str:
    """Register (idempotent) and return a valid JWT."""
    client.post("/api/auth/register", json={"email": email, "password": password})
    r = client.post("/api/auth/token", data={"username": email, "password": password})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["access_token"]


MOCK_ROWS = [
    {
        "ticker": "AAPL", "contract_type": "CALL", "strike": "185.00",
        "expiry": "2026-05-17", "premium": "125000.00", "trade_type": "SWEEP",
        "sentiment": "BULLISH", "influence_tier": "WHALE",
        "conviction_score": "0.900", "is_golden_sweep": True,
        "created_at": "2026-04-24T12:00:00Z",
    },
    {
        "ticker": "TSLA", "contract_type": "PUT", "strike": "250.00",
        "expiry": "2026-06-20", "premium": "88000.00", "trade_type": "BLOCK",
        "sentiment": "BEARISH", "influence_tier": "INSTITUTIONAL",
        "conviction_score": "0.750", "is_golden_sweep": False,
        "created_at": "2026-04-24T11:55:00Z",
    },
]


# ── flow/scan tests ───────────────────────────────────────────────────────────

class TestFlowScan:
    token: str

    def setup_method(self):
        self.token = register_and_login("flowtest@example.com")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    @patch("routers.flow._query_flow_events", new_callable=AsyncMock)
    def test_returns_all_tickers_when_no_ticker_param(self, mock_query):
        """GET /api/flow/scan with no ticker should return all events."""
        mock_query.return_value = (MOCK_ROWS, 2)
        r = client.get("/api/flow/scan", headers=self._headers())
        assert r.status_code == 200
        body = r.json()
        assert body["ticker"] is None            # no filter → ticker=null
        assert len(body["events"]) == 2
        assert body["total"] == 2
        # Both tickers present
        tickers = {e["ticker"] for e in body["events"]}
        assert "AAPL" in tickers
        assert "TSLA" in tickers
        # Verify mock was called with ticker=None
        mock_query.assert_called_once_with(None, 50, 0)

    @patch("routers.flow._query_flow_events", new_callable=AsyncMock)
    def test_filters_by_ticker(self, mock_query):
        """GET /api/flow/scan?ticker=AAPL should only return AAPL events."""
        aapl_rows = [MOCK_ROWS[0]]
        mock_query.return_value = (aapl_rows, 1)
        r = client.get("/api/flow/scan?ticker=AAPL", headers=self._headers())
        assert r.status_code == 200
        body = r.json()
        assert body["ticker"] == "AAPL"
        assert len(body["events"]) == 1
        assert body["events"][0]["ticker"] == "AAPL"
        # Verify uppercased ticker was passed
        mock_query.assert_called_once_with("AAPL", 50, 0)

    @patch("routers.flow._query_flow_events", new_callable=AsyncMock)
    def test_ticker_is_uppercased(self, mock_query):
        """Ticker param should be normalized to uppercase before DB query."""
        mock_query.return_value = ([], 0)
        client.get("/api/flow/scan?ticker=aapl", headers=self._headers())
        mock_query.assert_called_once_with("AAPL", 50, 0)

    @patch("routers.flow._query_flow_events", new_callable=AsyncMock)
    def test_empty_result_returns_200_not_404(self, mock_query):
        """Empty flow events should be 200 with empty list, not 404."""
        mock_query.return_value = ([], 0)
        r = client.get("/api/flow/scan?ticker=FAKEXXXX", headers=self._headers())
        assert r.status_code == 200
        body = r.json()
        assert body["events"] == []
        assert body["total"] == 0

    @patch("routers.flow._query_flow_events", new_callable=AsyncMock)
    def test_pagination_params_forwarded(self, mock_query):
        """limit and offset query params should be forwarded to DB query."""
        mock_query.return_value = ([], 0)
        client.get("/api/flow/scan?limit=10&offset=20", headers=self._headers())
        mock_query.assert_called_once_with(None, 10, 20)

    @patch("routers.flow._query_flow_events", new_callable=AsyncMock)
    def test_event_fields_serialized_correctly(self, mock_query):
        """FlowEventOut model should serialize all required fields."""
        mock_query.return_value = ([MOCK_ROWS[0]], 1)
        r = client.get("/api/flow/scan", headers=self._headers())
        assert r.status_code == 200
        event = r.json()["events"][0]
        required = [
            "ticker", "contract_type", "strike", "expiry", "premium",
            "trade_type", "sentiment", "influence_tier", "conviction_score",
            "is_golden_sweep", "timestamp"
        ]
        for field in required:
            assert field in event, f"Missing field: {field}"

    def test_requires_auth(self):
        """Flow scan should return 401 without a valid token."""
        r = client.get("/api/flow/scan")
        assert r.status_code == 401

    def test_invalid_token_rejected(self):
        """Flow scan should return 401 with a fake token."""
        r = client.get("/api/flow/scan", headers={"Authorization": "Bearer fakefakefake"})
        assert r.status_code == 401


# ── stream/stats tests ────────────────────────────────────────────────────────

class TestStreamStats:
    token: str

    def setup_method(self):
        self.token = register_and_login("statstest@example.com")

    def test_stats_returns_expected_shape(self):
        r = client.get("/api/stream/stats", headers={"Authorization": f"Bearer {self.token}"})
        assert r.status_code == 200
        body = r.json()
        assert "stats" in body
        stats = body["stats"]
        for field in ("active_symbols", "ticks", "classified", "signals", "errors"):
            assert field in stats, f"Missing stats field: {field}"

    def test_stats_requires_auth(self):
        r = client.get("/api/stream/stats")
        assert r.status_code == 401
