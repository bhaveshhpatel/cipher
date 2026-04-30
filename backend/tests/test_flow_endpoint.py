"""
Regression tests for routers/flow.py

Covers:
  - Unauthenticated request returns 401
  - Authenticated request returns FlowResponse shape (ticker, events, total, limit, offset)
  - ticker filter is uppercased
  - limit + offset forwarded to query function and echoed back
  - limit boundary: limit=1 and limit=200 both accepted
  - limit out of range: limit=0 → 422, limit=201 → 422
  - No Supabase env vars → empty list, not 500
  - direction→sentiment mapping: REPEAT_BUY→BULLISH, REPEAT_SELL→BEARISH, HOLD→NEUTRAL
  - alert_level→influence_tier mapping: CRITICAL→WHALE, HIGH→INSTITUTIONAL
  - conviction_score mapping from alert_level (CRITICAL→0.92)
  - is_accelerating=True → is_golden_sweep=True, trade_type='SWEEP'
  - is_accelerating=False → is_golden_sweep=False, trade_type='BLOCK'
  - Row with a parse error is skipped; rest of response still returned
  - Null-expiry / null-strike episode row IS included (valid aggregated episode)
  - Unknown direction/alert values fall back to NEUTRAL / RETAIL
"""
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from core.auth import create_access_token, TokenData
from main import app

client = TestClient(app)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_token() -> str:
    return create_access_token({"sub": "trader@cipher.io"})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_user():
    td = TokenData(email="trader@cipher.io", role="user")
    return patch("routers.flow.get_current_user", return_value=td)


def _mock_query(rows=None, total=0):
    return patch(
        "routers.flow._query_flow_episodes",
        new=AsyncMock(return_value=(rows or [], total)),
    )


def _sample_row(
    ticker="AAPL",
    direction="BULLISH",
    alert_level="HIGH",
    is_accelerating=False,
) -> dict:
    return {
        "id":            1,
        "ticker":        ticker,
        "direction":     direction,
        "contract_type": "CALL",
        "strike":        195.0,
        "expiry":        "2026-06-20",
        "total_premium": 250_000.0,
        "trade_count":   8,
        "alert_level":   alert_level,
        "is_accelerating": is_accelerating,
        "signal_ts":     "2026-04-25T18:00:00Z",
        "created_at":    "2026-04-25T18:00:01Z",
    }


# ── auth guard ────────────────────────────────────────────────────────────────

def test_flow_scan_unauthenticated_returns_401():
    resp = client.get("/api/flow/scan")
    assert resp.status_code == 401


# ── happy path ────────────────────────────────────────────────────────────────

def test_flow_scan_returns_valid_response_shape():
    row = _sample_row()
    with _mock_user(), _mock_query([row], total=1):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    assert resp.status_code == 200
    body = resp.json()
    for key in ("ticker", "events", "total", "limit", "offset"):
        assert key in body
    assert body["total"] == 1
    assert len(body["events"]) == 1
    ev = body["events"][0]
    for key in ("ticker", "contract_type", "strike", "expiry", "premium",
                "trade_type", "sentiment", "influence_tier", "conviction_score",
                "is_golden_sweep"):
        assert key in ev


def test_flow_scan_empty_returns_zero_events():
    with _mock_user(), _mock_query([], total=0):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["total"] == 0


# ── ticker normalisation ─────────────────────────────────────────────────────

def test_flow_scan_ticker_is_uppercased():
    captured = {}

    async def capture(ticker, limit, offset):
        captured["ticker"] = ticker
        return [], 0

    with _mock_user():
        with patch("routers.flow._query_flow_episodes", new=AsyncMock(side_effect=capture)):
            client.get("/api/flow/scan?ticker=tsla", headers=_auth(_make_token()))
    assert captured["ticker"] == "TSLA"


# ── pagination ───────────────────────────────────────────────────────────────

def test_flow_scan_pagination_params_forwarded():
    captured = {}

    async def capture(ticker, limit, offset):
        captured["limit"] = limit
        captured["offset"] = offset
        return [], 0

    with _mock_user():
        with patch("routers.flow._query_flow_episodes", new=AsyncMock(side_effect=capture)):
            resp = client.get(
                "/api/flow/scan?limit=25&offset=75",
                headers=_auth(_make_token()),
            )
    assert resp.status_code == 200
    assert captured["limit"] == 25
    assert captured["offset"] == 75


def test_flow_scan_response_echoes_limit_offset():
    with _mock_user(), _mock_query([], total=0):
        resp = client.get(
            "/api/flow/scan?limit=10&offset=20",
            headers=_auth(_make_token()),
        )
    body = resp.json()
    assert body["limit"] == 10
    assert body["offset"] == 20


def test_flow_scan_limit_min_boundary():
    with _mock_user(), _mock_query():
        resp = client.get("/api/flow/scan?limit=1", headers=_auth(_make_token()))
    assert resp.status_code == 200


def test_flow_scan_limit_max_boundary():
    with _mock_user(), _mock_query():
        resp = client.get("/api/flow/scan?limit=200", headers=_auth(_make_token()))
    assert resp.status_code == 200


def test_flow_scan_limit_zero_returns_422():
    with _mock_user(), _mock_query():
        resp = client.get("/api/flow/scan?limit=0", headers=_auth(_make_token()))
    assert resp.status_code == 422


def test_flow_scan_limit_over_max_returns_422():
    with _mock_user(), _mock_query():
        resp = client.get("/api/flow/scan?limit=201", headers=_auth(_make_token()))
    assert resp.status_code == 422


# ── no env vars → empty result, not 500 ───────────────────────────────────────────

def test_flow_scan_no_supabase_env_returns_empty_not_500():
    with _mock_user():
        with patch("routers.flow._SUPABASE_URL", None), \
             patch("routers.flow._SUPABASE_KEY", None):
            resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    assert resp.status_code == 200
    assert resp.json()["events"] == []


# ── direction → sentiment mapping ────────────────────────────────────────────────

@pytest.mark.parametrize("direction,expected_sentiment", [
    ("REPEAT_BUY",  "BULLISH"),
    ("REPEAT_SELL", "BEARISH"),
    ("BULLISH",     "BULLISH"),
    ("BEARISH",     "BEARISH"),
    ("NEUTRAL",     "NEUTRAL"),
    ("HOLD",        "NEUTRAL"),
    ("UNKNOWN_VAL", "NEUTRAL"),  # unmapped falls back to NEUTRAL
])
def test_flow_direction_to_sentiment_mapping(direction, expected_sentiment):
    row = _sample_row(direction=direction)
    with _mock_user(), _mock_query([row], total=1):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    assert resp.status_code == 200
    assert resp.json()["events"][0]["sentiment"] == expected_sentiment


# ── alert_level → influence_tier + conviction_score mapping ────────────────────────

@pytest.mark.parametrize("alert,expected_tier,expected_conviction", [
    ("CRITICAL", "WHALE",         0.92),
    ("HIGH",     "INSTITUTIONAL", 0.75),
    ("MEDIUM",   "LARGE",         0.55),
    ("LOW",      "RETAIL",        0.35),
    ("UNKNOWN",  "RETAIL",        0.5),  # unmapped falls back to RETAIL / 0.5
])
def test_flow_alert_level_mapping(alert, expected_tier, expected_conviction):
    row = _sample_row(alert_level=alert)
    with _mock_user(), _mock_query([row], total=1):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    assert resp.status_code == 200
    ev = resp.json()["events"][0]
    assert ev["influence_tier"] == expected_tier
    assert pytest.approx(ev["conviction_score"], abs=0.01) == expected_conviction


# ── is_accelerating flag ─────────────────────────────────────────────────────────

def test_flow_is_accelerating_true_sets_golden_sweep_and_sweep_type():
    row = _sample_row(is_accelerating=True)
    with _mock_user(), _mock_query([row], total=1):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    ev = resp.json()["events"][0]
    assert ev["is_golden_sweep"] is True
    assert ev["trade_type"] == "SWEEP"


def test_flow_is_accelerating_false_sets_block_type():
    row = _sample_row(is_accelerating=False)
    with _mock_user(), _mock_query([row], total=1):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    ev = resp.json()["events"][0]
    assert ev["is_golden_sweep"] is False
    assert ev["trade_type"] == "BLOCK"


# ── parse-error row is skipped (except path) ──────────────────────────────────────

def test_flow_scan_parse_error_row_is_skipped():
    """
    A row whose fields cause a hard parse error (strike='NOT_A_FLOAT' triggers
    float() ValueError) must be skipped via the except handler while the valid
    row that follows is still returned.

    Note: a row with null expiry/strike is NOT a parse error — those are valid
    aggregated episode rows (see test_flow_scan_null_expiry_row_is_included).
    """
    bad_row = {
        "id":            99,
        "ticker":        "BAD",
        "direction":     "BULLISH",
        "contract_type": "CALL",
        "strike":        "NOT_A_FLOAT",  # will raise ValueError in float()
        "expiry":        "2026-06-20",
        "total_premium": 100_000.0,
        "trade_count":   5,
        "alert_level":   "LOW",
        "is_accelerating": False,
        "signal_ts":     None,
        "created_at":    "2026-04-30T10:00:00Z",
    }
    good_row = _sample_row(ticker="SPY")
    with _mock_user(), _mock_query([bad_row, good_row], total=2):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["ticker"] == "SPY"


# ── null-expiry / null-strike episode rows are included ───────────────────────────

def test_flow_scan_null_expiry_row_is_included():
    """
    flow_episodes rows legitimately have null expiry/strike when the episode
    spans multiple contracts or is synthetic (e.g. SPY REPEAT_SELL episodes).
    These rows must NOT be dropped — FlowEventOut.expiry/strike are Optional.
    """
    null_expiry_row = {
        "id":            357290,
        "ticker":        "SPY",
        "direction":     "REPEAT_SELL",
        "contract_type": "PUT",
        "strike":        None,
        "expiry":        None,
        "total_premium": 259_857.0,
        "trade_count":   125,
        "alert_level":   "ALERT",
        "is_accelerating": True,
        "signal_ts":     "2026-04-29T20:14:48Z",
        "created_at":    "2026-04-29T20:15:17Z",
    }
    with _mock_user(), _mock_query([null_expiry_row], total=1):
        resp = client.get("/api/flow/scan", headers=_auth(_make_token()))
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["ticker"] == "SPY"
    assert ev["expiry"] is None
    assert ev["strike"] is None
    assert ev["premium"] == 259_857.0
    assert ev["is_golden_sweep"] is True
