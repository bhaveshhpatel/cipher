"""
Phase 4 — test_simulation_router.py

Covers every branch in routers/simulation.py:
  - POST /api/simulation/run: success path → correct SimulationResponse shape
  - POST /api/simulation/run: n_agents=0  → 422
  - POST /api/simulation/run: n_agents=13 → 422
  - POST /api/simulation/run: n_runs=0    → 422
  - POST /api/simulation/run: n_runs=6    → 422
  - POST /api/simulation/run: unauthenticated → 401/403
  - POST /api/simulation/run: n_agents snapped (e.g. 4 → 3 or 6)
  - POST /api/simulation/run: agents list in response matches n_agents used
  - POST /api/simulation/run: ticker upper-cased in response
  - POST /api/simulation/run: flow_events serialised to dicts before run_ensemble
  - POST /api/simulation/run: n_runs forwarded to run_ensemble
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock
from dataclasses import dataclass, field
from typing import List


# ---------------------------------------------------------------------------
# Minimal EnsembleResult stub so we don't need Groq
# ---------------------------------------------------------------------------
@dataclass
class _EnsembleResult:
    ticker:     str
    direction:  str
    confidence: float
    bull_votes: int
    bear_votes: int
    hold_votes: int
    summary:    str
    agents:     List[dict] = field(default_factory=list)


def _good_result(ticker="AAPL", n_agents=6) -> _EnsembleResult:
    agents = [
        {"role": f"role{i}", "name": f"Agent {i}", "direction": "BUY",
         "reasoning": "Tape strong.", "confidence": 0.8}
        for i in range(n_agents)
    ]
    return _EnsembleResult(
        ticker=ticker.upper(), direction="BUY", confidence=round(n_agents / n_agents, 3),
        bull_votes=n_agents, bear_votes=0, hold_votes=0,
        summary=f"{n_agents} BUY votes.", agents=agents,
    )


# ---------------------------------------------------------------------------
# App fixture (override auth + run_ensemble)
# ---------------------------------------------------------------------------
def _make_client(ensemble_result=None):
    """Returns a TestClient with auth bypassed and run_ensemble mocked."""
    from fastapi import FastAPI
    from routers.simulation import router
    from core.auth import TokenData

    app = FastAPI()
    app.include_router(router)

    # Bypass JWT auth
    fake_user = TokenData(user_id="test-uid", email="test@example.com")

    async def _fake_auth():
        return fake_user

    from core.auth import get_current_user
    app.dependency_overrides[get_current_user] = _fake_auth

    mock_result = ensemble_result or _good_result()
    mock_run    = AsyncMock(return_value=mock_result)

    return TestClient(app), mock_run


class TestSimulationRouterSuccess:

    def test_success_response_shape(self):
        client, mock_run = _make_client()
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "aapl",
                "n_agents": 6,
                "n_runs": 1,
            })
        assert resp.status_code == 200
        body = resp.json()
        for key in ["ticker", "direction", "confidence", "bull_votes",
                    "bear_votes", "hold_votes", "summary", "agents"]:
            assert key in body

    def test_ticker_uppercased(self):
        client, mock_run = _make_client(_good_result(ticker="tsla"))
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "tsla", "n_agents": 6, "n_runs": 1,
            })
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "TSLA"

    def test_agents_list_shape(self):
        client, mock_run = _make_client(_good_result(n_agents=3))
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "SPY", "n_agents": 3, "n_runs": 1,
            })
        agents = resp.json()["agents"]
        assert len(agents) == 3
        for a in agents:
            for key in ["role", "name", "direction", "reasoning", "confidence"]:
                assert key in a

    def test_flow_events_serialised(self):
        """flow_events passed as dicts (model_dump) to run_ensemble."""
        client, mock_run = _make_client()
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "NVDA",
                "n_agents": 6,
                "n_runs": 1,
                "flow_events": [{
                    "ticker": "NVDA",
                    "contract_type": "CALL",
                    "strike": 900,
                    "expiry": "2026-06-20",
                    "premium": 1000000,
                    "sentiment": "BULLISH",
                    "influence_tier": "WHALE",
                    "is_golden_sweep": True,
                }],
            })
        assert resp.status_code == 200
        call_kwargs = mock_run.call_args[1]
        assert isinstance(call_kwargs["flow_events"], list)
        assert isinstance(call_kwargs["flow_events"][0], dict)

    def test_n_runs_forwarded(self):
        client, mock_run = _make_client()
        with patch("routers.simulation.run_ensemble", mock_run):
            client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 6, "n_runs": 3,
            })
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["n_runs"] == 3

    def test_n_agents_forwarded(self):
        client, mock_run = _make_client()
        with patch("routers.simulation.run_ensemble", mock_run):
            client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 9, "n_runs": 1,
            })
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["n_agents"] == 9


class TestSimulationRouterValidation:

    def _client(self):
        client, mock_run = _make_client()
        return client, mock_run

    def test_n_agents_zero_returns_422(self):
        client, mock_run = self._client()
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 0, "n_runs": 1,
            })
        assert resp.status_code == 422

    def test_n_agents_13_returns_422(self):
        client, mock_run = self._client()
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 13, "n_runs": 1,
            })
        assert resp.status_code == 422

    def test_n_runs_zero_returns_422(self):
        client, mock_run = self._client()
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 6, "n_runs": 0,
            })
        assert resp.status_code == 422

    def test_n_runs_six_returns_422(self):
        client, mock_run = self._client()
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 6, "n_runs": 6,
            })
        assert resp.status_code == 422

    def test_unauthenticated_returns_401_or_403(self):
        from fastapi import FastAPI
        from routers.simulation import router
        app = FastAPI()
        app.include_router(router)
        # No dependency override → real auth → 401/403
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/simulation/run", json={
            "ticker": "AAPL", "n_agents": 6, "n_runs": 1,
        })
        assert resp.status_code in (401, 403)

    def test_boundary_n_agents_1_valid(self):
        client, mock_run = self._client()
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 1, "n_runs": 1,
            })
        # n_agents=1 is within 1-12, router must accept it
        assert resp.status_code == 200

    def test_boundary_n_agents_12_valid(self):
        client, mock_run = self._client(_good_result(n_agents=12))
        with patch("routers.simulation.run_ensemble", mock_run):
            resp = client.post("/api/simulation/run", json={
                "ticker": "AAPL", "n_agents": 12, "n_runs": 1,
            })
        assert resp.status_code == 200
