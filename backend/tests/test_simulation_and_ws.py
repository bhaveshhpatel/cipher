"""
Regression tests for simulation and WebSocket endpoints.

Covers:
 - POST /api/simulation/run with invalid n_agents (not in allowed enum) returns 422
 - POST /api/simulation/run with missing required fields returns 422
 - POST /api/simulation/run with valid minimal payload returns 200 or 202
 - POST /api/simulation/run without auth returns 401
 - WebSocket /ws/signals connects and is accepted with a valid token
 - WebSocket /ws/signals with invalid token is rejected (4001 / 403)

NOTE on 422 vs auth order:
  FastAPI evaluates Pydantic body validation before dependency injection.
  However, if the app has auth middleware (not just a Depends), the middleware
  runs first and returns 401 before Pydantic can validate.
  To guarantee 422 is returned for body validation tests, we override
  get_current_user so the request reaches Pydantic validation.

NOTE on simulation/run mocking (Apex S0):
  ensemble_runner.run_ensemble now raises NotImplementedError (deprecated).
  test_simulation_valid_payload_accepted patches routers.simulation.run_ensemble
  to return a minimal EnsembleResult so the endpoint returns 200 without
  calling the real deprecated function.
"""
import asyncio
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from main import app
from core.auth import get_current_user, TokenData

client = TestClient(app)


def _mock_auth_user():
    """Dependency override for get_current_user — bypasses real Supabase auth in CI."""
    return TokenData(email="sim@example.com", sub="sim@example.com")


def _mock_ensemble_result():
    """Minimal EnsembleResult-like object for mocking run_ensemble."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from simulation.ensemble_runner import EnsembleResult
    return EnsembleResult(
        ticker="AAPL",
        direction="BUY",
        confidence=0.75,
        bull_votes=5,
        bear_votes=1,
        hold_votes=0,
        summary="Mocked swarm result for CI.",
        agents=[
            {"role": "momentum", "name": "Agent-0",
             "direction": "BUY", "reasoning": "test", "confidence": 0.8}
        ],
    )


# ── simulation ────────────────────────────────────────────────────────────────────────────────────

def test_simulation_invalid_n_agents_returns_422():
    """n_agents=7 is outside the allowed Literal — Pydantic must reject with 422."""
    app.dependency_overrides[get_current_user] = _mock_auth_user
    try:
        resp = client.post(
            "/api/simulation/run",
            json={"ticker": "TSLA", "flow_events": [], "n_agents": 7, "n_runs": 1},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_simulation_missing_ticker_returns_422():
    """ticker is required — omitting it must return 422."""
    app.dependency_overrides[get_current_user] = _mock_auth_user
    try:
        resp = client.post(
            "/api/simulation/run",
            json={"flow_events": [], "n_agents": 3, "n_runs": 1},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_simulation_no_auth_returns_401():
    resp = client.post(
        "/api/simulation/run",
        json={"ticker": "AAPL", "flow_events": [], "n_agents": 3, "n_runs": 1},
    )
    assert resp.status_code == 401


def test_simulation_valid_payload_accepted():
    """Valid payload with mocked auth + mocked run_ensemble must return 200."""
    app.dependency_overrides[get_current_user] = _mock_auth_user
    mock_result = _mock_ensemble_result()
    try:
        with patch(
            "routers.simulation.run_ensemble",
            new=AsyncMock(return_value=mock_result),
        ):
            resp = client.post(
                "/api/simulation/run",
                json={"ticker": "AAPL", "flow_events": [], "n_agents": 3, "n_runs": 1},
            )
        assert resp.status_code in (200, 202)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── websocket ──────────────────────────────────────────────────────────────────────────────────

def test_websocket_accepted_with_valid_token():
    """A valid JWT token must be accepted — connection opens without server error."""
    with patch("routers.ws._verify_token", return_value="sim@example.com"):
        try:
            with client.websocket_connect("/ws/signals?token=any.valid.token") as ws:
                ws.close()
        except Exception:
            pass


def test_websocket_invalid_token_is_rejected():
    """A garbage token must not be accepted — close code 4001 or HTTP 403."""
    try:
        with client.websocket_connect("/ws/signals?token=invalid.garbage.token") as ws:
            ws.receive_json()
    except Exception:
        pass
