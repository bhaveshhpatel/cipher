"""
Regression tests for routers/simulation.py

Strategy:
  - Override get_current_user so all auth-passing tests skip JWT.
  - run_ensemble patched to avoid any real agent/swarm execution.
  - Tests the router's validation, request marshalling, and response mapping.

Covers:
  Auth guard:
  - POST /api/simulation/run without auth -> 401

  Validation:
  - n_agents < 1 -> 422
  - n_agents > 12 -> 422
  - n_agents boundary 1 and 12 -> accepted
  - n_runs < 1 -> 422
  - n_runs > 5 -> 422
  - n_runs boundary 1 and 5 -> accepted

  Request marshalling:
  - ticker forwarded as uppercase to run_ensemble
  - flow_events forwarded as list of dicts (model_dump)
  - n_agents and n_runs forwarded
  - empty flow_events list accepted

  Response mapping:
  - SimulationResponse shape: ticker/direction/confidence/
    bull_votes/bear_votes/hold_votes/summary/agents
  - agents mapped to AgentOut: role/name/direction/reasoning/confidence
  - run_ensemble exception -> 500

  Model defaults:
  - FlowEventIn: contract_type='CALL', sentiment='NEUTRAL',
    influence_tier='RETAIL', conviction_score=0.5,
    is_golden_sweep=False, ticker='', strike=0, premium=0
  - SimulationRequest: n_agents=6, n_runs=1
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

from core.auth import get_current_user, TokenData
from routers.simulation import router, FlowEventIn, SimulationRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if authenticated:
        async def _auth():
            return TokenData(email="sim@cipher.app", role="user")
        app.dependency_overrides[get_current_user] = _auth
    return app


def _make_ensemble_result(
    ticker="AAPL",
    direction="BULLISH",
    confidence=0.78,
    bull_votes=4,
    bear_votes=1,
    hold_votes=1,
    summary="Strong bullish consensus",
    agents=None,
) -> SimpleNamespace:
    if agents is None:
        agents = [{
            "role":       "momentum_trader",
            "name":       "Agent-1",
            "direction":  "BULLISH",
            "reasoning":  "Strong flow",
            "confidence": 0.82,
        }]
    return SimpleNamespace(
        ticker=ticker, direction=direction, confidence=confidence,
        bull_votes=bull_votes, bear_votes=bear_votes, hold_votes=hold_votes,
        summary=summary, agents=agents,
    )


@pytest.fixture
def client():
    return TestClient(_make_app(authenticated=True))


@pytest.fixture
def raw_client():
    return TestClient(_make_app(authenticated=False), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_no_auth_returns_401(raw_client):
    resp = raw_client.post("/api/simulation/run", json={"ticker": "AAPL"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# n_agents validation
# ---------------------------------------------------------------------------

def test_n_agents_zero_returns_422(client):
    resp = client.post("/api/simulation/run", json={"ticker": "AAPL", "n_agents": 0})
    assert resp.status_code == 422


def test_n_agents_13_returns_422(client):
    resp = client.post("/api/simulation/run", json={"ticker": "AAPL", "n_agents": 13})
    assert resp.status_code == 422


@pytest.mark.parametrize("n", [1, 12])
def test_n_agents_boundary_accepted(client, n):
    with patch("routers.simulation.run_ensemble", new_callable=AsyncMock,
               return_value=_make_ensemble_result()):
        resp = client.post("/api/simulation/run", json={"ticker": "AAPL", "n_agents": n})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# n_runs validation
# ---------------------------------------------------------------------------

def test_n_runs_zero_returns_422(client):
    resp = client.post("/api/simulation/run", json={"ticker": "AAPL", "n_runs": 0})
    assert resp.status_code == 422


def test_n_runs_6_returns_422(client):
    resp = client.post("/api/simulation/run", json={"ticker": "AAPL", "n_runs": 6})
    assert resp.status_code == 422


@pytest.mark.parametrize("r", [1, 5])
def test_n_runs_boundary_accepted(client, r):
    with patch("routers.simulation.run_ensemble", new_callable=AsyncMock,
               return_value=_make_ensemble_result()):
        resp = client.post("/api/simulation/run", json={"ticker": "AAPL", "n_runs": r})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Request marshalling
# ---------------------------------------------------------------------------

def test_ticker_uppercased_in_ensemble_call(client):
    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return _make_ensemble_result(ticker=kwargs["ticker"])

    with patch("routers.simulation.run_ensemble", side_effect=_capture):
        client.post("/api/simulation/run", json={"ticker": "tsla"})

    assert captured["ticker"] == "TSLA"


def test_flow_events_forwarded_as_dicts(client):
    captured = {}
    event = {"ticker": "AAPL", "contract_type": "CALL", "strike": 200.0,
              "expiry": "2026-05-16", "premium": 5.0,
              "trade_type": "SWEEP", "sentiment": "BULLISH",
              "influence_tier": "WHALE", "conviction_score": 0.9,
              "is_golden_sweep": True, "timestamp": "2026-04-25T18:00:00Z"}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return _make_ensemble_result()

    with patch("routers.simulation.run_ensemble", side_effect=_capture):
        client.post("/api/simulation/run", json={"ticker": "AAPL", "flow_events": [event]})

    assert isinstance(captured["flow_events"], list)
    assert isinstance(captured["flow_events"][0], dict)
    assert captured["flow_events"][0]["influence_tier"] == "WHALE"


def test_empty_flow_events_accepted(client):
    with patch("routers.simulation.run_ensemble", new_callable=AsyncMock,
               return_value=_make_ensemble_result()):
        resp = client.post("/api/simulation/run", json={"ticker": "MSFT", "flow_events": []})
    assert resp.status_code == 200


def test_n_agents_and_n_runs_forwarded(client):
    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return _make_ensemble_result()

    with patch("routers.simulation.run_ensemble", side_effect=_capture):
        client.post("/api/simulation/run", json={"ticker": "NVDA", "n_agents": 9, "n_runs": 3})

    assert captured["n_agents"] == 9
    assert captured["n_runs"]   == 3


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------

def test_simulation_response_shape(client):
    with patch("routers.simulation.run_ensemble", new_callable=AsyncMock,
               return_value=_make_ensemble_result()):
        resp = client.post("/api/simulation/run", json={"ticker": "AAPL"})
    body = resp.json()
    for key in ("ticker", "direction", "confidence",
                "bull_votes", "bear_votes", "hold_votes", "summary", "agents"):
        assert key in body


def test_agent_out_shape(client):
    with patch("routers.simulation.run_ensemble", new_callable=AsyncMock,
               return_value=_make_ensemble_result()):
        resp = client.post("/api/simulation/run", json={"ticker": "AAPL"})
    agent = resp.json()["agents"][0]
    for key in ("role", "name", "direction", "reasoning", "confidence"):
        assert key in agent


def test_ticker_echoed_in_response(client):
    with patch("routers.simulation.run_ensemble", new_callable=AsyncMock,
               return_value=_make_ensemble_result(ticker="TSLA")):
        resp = client.post("/api/simulation/run", json={"ticker": "tsla"})
    assert resp.json()["ticker"] == "TSLA"


def test_run_ensemble_exception_returns_500(client):
    async def _explode(**kwargs):
        raise RuntimeError("LLM timeout")

    with patch("routers.simulation.run_ensemble", side_effect=_explode):
        resp = client.post("/api/simulation/run", json={"ticker": "AAPL"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Model defaults
# ---------------------------------------------------------------------------

def test_flow_event_defaults():
    e = FlowEventIn()
    assert e.contract_type    == "CALL"
    assert e.sentiment        == "NEUTRAL"
    assert e.influence_tier   == "RETAIL"
    assert e.conviction_score == 0.5
    assert e.is_golden_sweep  is False
    assert e.strike           == 0
    assert e.premium          == 0
    assert e.ticker           == ""


def test_simulation_request_defaults():
    r = SimulationRequest(ticker="AAPL")
    assert r.n_agents == 6
    assert r.n_runs   == 1
