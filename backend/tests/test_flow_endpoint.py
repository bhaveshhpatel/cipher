"""
Tests for the /api/flow/scan endpoint.
Run with: pytest backend/tests/test_flow_endpoint.py -v
"""
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


# ── Fixtures ───────────────────────────────────────────────────────────────

MOCK_EPISODES = [
    {
        "id": 1,
        "ticker": "AAPL",
        "direction": "REPEAT_BUY",
        "contract_type": "CALL",
        "strike": "195.00",
        "expiry": "2025-05-16",
        "total_premium": "2850000.00",
        "trade_count": 12,
        "alert_level": "CRITICAL",
        "is_accelerating": True,
        "signal_ts": "2026-04-24T10:00:00+00:00",
        "created_at": "2026-04-24T10:00:00+00:00",
    },
    {
        "id": 2,
        "ticker": "TSLA",
        "direction": "REPEAT_SELL",
        "contract_type": "PUT",
        "strike": "175.00",
        "expiry": "2025-05-09",
        "total_premium": "3400000.00",
        "trade_count": 8,
        "alert_level": "HIGH",
        "is_accelerating": False,
        "signal_ts": "2026-04-24T09:55:00+00:00",
        "created_at": "2026-04-24T09:55:00+00:00",
    },
]


@pytest.fixture
def mock_supabase_episodes():
    """Patch _query_flow_episodes to return mock data."""
    with patch(
        "routers.flow._query_flow_episodes",
        new_callable=AsyncMock,
        return_value=(MOCK_EPISODES, 2),
    ) as mock:
        yield mock


# ── Tests ──────────────────────────────────────────────────────────────────

class TestFlowDirectionMapping:
    """Unit tests for direction → sentiment mapping."""

    def test_repeat_buy_maps_to_bullish(self):
        from routers.flow import _DIRECTION_TO_SENTIMENT
        assert _DIRECTION_TO_SENTIMENT["REPEAT_BUY"] == "BULLISH"

    def test_repeat_sell_maps_to_bearish(self):
        from routers.flow import _DIRECTION_TO_SENTIMENT
        assert _DIRECTION_TO_SENTIMENT["REPEAT_SELL"] == "BEARISH"

    def test_neutral_maps_to_neutral(self):
        from routers.flow import _DIRECTION_TO_SENTIMENT
        assert _DIRECTION_TO_SENTIMENT["NEUTRAL"] == "NEUTRAL"

    def test_bullish_direct_maps_correctly(self):
        from routers.flow import _DIRECTION_TO_SENTIMENT
        assert _DIRECTION_TO_SENTIMENT["BULLISH"] == "BULLISH"


class TestFlowAlertTierMapping:
    """Unit tests for alert_level → influence_tier mapping."""

    def test_critical_maps_to_whale(self):
        from routers.flow import _ALERT_TO_TIER
        assert _ALERT_TO_TIER["CRITICAL"] == "WHALE"

    def test_high_maps_to_institutional(self):
        from routers.flow import _ALERT_TO_TIER
        assert _ALERT_TO_TIER["HIGH"] == "INSTITUTIONAL"

    def test_medium_maps_to_large(self):
        from routers.flow import _ALERT_TO_TIER
        assert _ALERT_TO_TIER["MEDIUM"] == "LARGE"

    def test_low_maps_to_retail(self):
        from routers.flow import _ALERT_TO_TIER
        assert _ALERT_TO_TIER["LOW"] == "RETAIL"


class TestFlowScanEndpoint:
    """Integration tests for GET /api/flow/scan."""

    def _get_client(self):
        """Create test client with auth bypassed."""
        from main import app
        from core.auth import get_current_user
        from pydantic import BaseModel

        class FakeToken(BaseModel):
            sub: str = "test@example.com"

        app.dependency_overrides[get_current_user] = lambda: FakeToken()
        return TestClient(app)

    def test_scan_returns_all_events_no_ticker(self, mock_supabase_episodes):
        """Flow Scanner: no ticker param returns all episodes."""
        client = self._get_client()
        resp = client.get("/api/flow/scan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["events"]) == 2
        assert data["ticker"] is None

    def test_scan_ticker_filter_passed_to_supabase(self, mock_supabase_episodes):
        """Ticker filter is forwarded correctly to Supabase query."""
        client = self._get_client()
        resp = client.get("/api/flow/scan?ticker=AAPL")
        assert resp.status_code == 200
        # Verify the mock was called with ticker="AAPL"
        call_kwargs = mock_supabase_episodes.call_args
        assert call_kwargs[0][0] == "AAPL"  # first positional arg

    def test_scan_event_sentiment_mapped_correctly(self, mock_supabase_episodes):
        """REPEAT_BUY direction becomes BULLISH sentiment in response."""
        client = self._get_client()
        resp = client.get("/api/flow/scan")
        events = resp.json()["events"]
        aapl_event = next(e for e in events if e["ticker"] == "AAPL")
        assert aapl_event["sentiment"] == "BULLISH"

    def test_scan_event_tier_mapped_correctly(self, mock_supabase_episodes):
        """CRITICAL alert_level becomes WHALE influence_tier in response."""
        client = self._get_client()
        resp = client.get("/api/flow/scan")
        events = resp.json()["events"]
        aapl_event = next(e for e in events if e["ticker"] == "AAPL")
        assert aapl_event["influence_tier"] == "WHALE"

    def test_scan_golden_sweep_from_is_accelerating(self, mock_supabase_episodes):
        """is_accelerating=True maps to is_golden_sweep=True."""
        client = self._get_client()
        resp = client.get("/api/flow/scan")
        events = resp.json()["events"]
        aapl_event = next(e for e in events if e["ticker"] == "AAPL")
        tsla_event = next(e for e in events if e["ticker"] == "TSLA")
        assert aapl_event["is_golden_sweep"] is True
        assert tsla_event["is_golden_sweep"] is False

    def test_scan_bearish_event(self, mock_supabase_episodes):
        """REPEAT_SELL direction → BEARISH sentiment, HIGH alert → INSTITUTIONAL tier."""
        client = self._get_client()
        resp = client.get("/api/flow/scan")
        events = resp.json()["events"]
        tsla_event = next(e for e in events if e["ticker"] == "TSLA")
        assert tsla_event["sentiment"] == "BEARISH"
        assert tsla_event["influence_tier"] == "INSTITUTIONAL"

    def test_scan_requires_auth(self):
        """Unauthenticated request returns 401/403."""
        from main import app
        from core.auth import get_current_user
        app.dependency_overrides.pop(get_current_user, None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/flow/scan")
        assert resp.status_code in (401, 403)

    def test_scan_pagination_params(self, mock_supabase_episodes):
        """limit and offset are forwarded to query."""
        client = self._get_client()
        resp = client.get("/api/flow/scan?limit=10&offset=20")
        assert resp.status_code == 200
        call_kwargs = mock_supabase_episodes.call_args
        assert call_kwargs[0][1] == 10   # limit
        assert call_kwargs[0][2] == 20   # offset
