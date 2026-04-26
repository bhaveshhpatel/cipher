"""
Coverage boost for simulation/swarm_engine.py.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from simulation.swarm_engine import (
    SwarmEngine,
    _build_flow_summary,
    _resolve_n_agents,
    _run_agent,
    AGENT_ROLES,
)


# --- _resolve_n_agents ---

def test_resolve_snaps_to_3():
    assert _resolve_n_agents(1) == 3

def test_resolve_snaps_to_6():
    assert _resolve_n_agents(5) == 6

def test_resolve_exact_9():
    assert _resolve_n_agents(9) == 9

def test_resolve_clamps_max_12():
    assert _resolve_n_agents(100) == 12

def test_resolve_snaps_12_from_11():
    assert _resolve_n_agents(11) == 12


# --- _build_flow_summary ---

def test_flow_summary_string_passthrough():
    assert _build_flow_summary("already a string") == "already a string"

def test_flow_summary_empty_list():
    assert _build_flow_summary([]) == "No flow events provided."

def test_flow_summary_single_event():
    ev = {
        "ticker": "AAPL", "contract_type": "CALL",
        "premium": 500_000, "strike": 180.0,
        "expiry": "2026-06-20", "sentiment": "BULLISH",
        "influence_tier": "WHALE", "is_golden_sweep": False,
    }
    s = _build_flow_summary([ev])
    assert "AAPL" in s
    assert "CALL" in s
    assert "500,000" in s

def test_flow_summary_golden_sweep_label():
    ev = {
        "ticker": "TSLA", "contract_type": "PUT",
        "premium": 1_000_000, "strike": 200.0,
        "expiry": "2026-07-18", "sentiment": "BEARISH",
        "influence_tier": "INSTITUTIONAL", "is_golden_sweep": True,
    }
    s = _build_flow_summary([ev])
    assert "GOLDEN SWEEP" in s

def test_flow_summary_caps_at_20_events():
    events = [
        {"ticker": "SPY", "contract_type": "CALL", "premium": 100_000,
         "strike": 500, "expiry": "2026-05-01", "sentiment": "BULLISH",
         "influence_tier": "RETAIL", "is_golden_sweep": False}
        for _ in range(25)
    ]
    s = _build_flow_summary(events)
    lines = [ln for ln in s.splitlines() if ln.strip()]
    assert len(lines) == 20


# --- SwarmEngine no-client path ---

def test_swarm_engine_no_api_key_run():
    with patch("simulation.swarm_engine.settings") as mock_settings:
        mock_settings.GROQ_API_KEY = ""
        mock_settings.SWARM_N_AGENTS = 6
        engine = SwarmEngine(n_agents=6)
        assert engine.client is None

    async def _run():
        return await engine.run("AAPL", [{"ticker": "AAPL", "premium": 100_000,
                                          "contract_type": "CALL", "strike": 180,
                                          "expiry": "2026-06-20", "sentiment": "BULLISH",
                                          "influence_tier": "WHALE", "is_golden_sweep": False}])

    verdicts = asyncio.get_event_loop().run_until_complete(_run())
    assert len(verdicts) == 6
    assert all(v.direction == "HOLD" for v in verdicts)
    assert all("GROQ_API_KEY" in v.reasoning for v in verdicts)

def test_swarm_engine_no_client_3_agents():
    with patch("simulation.swarm_engine.settings") as mock_settings:
        mock_settings.GROQ_API_KEY = ""
        mock_settings.SWARM_N_AGENTS = 3
        engine = SwarmEngine(n_agents=3)

    async def _run():
        return await engine.run("NVDA", [])

    verdicts = asyncio.get_event_loop().run_until_complete(_run())
    assert len(verdicts) == 3


# --- _run_agent: LLM response parsing ---

def _make_response(text: str):
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _mock_client(response_text: str):
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_make_response(response_text))
    return client


def test_run_agent_parses_buy():
    client = _mock_client(
        "VERDICT: BUY\nREASONING: Strong whale flow\nCONFIDENCE: 0.85"
    )

    async def _run():
        return await _run_agent(client, AGENT_ROLES[0], "AAPL", "flow summary")

    v = asyncio.get_event_loop().run_until_complete(_run())
    assert v.direction  == "BUY"
    assert v.reasoning  == "Strong whale flow"
    assert v.confidence == pytest.approx(0.85)


def test_run_agent_parses_sell():
    client = _mock_client(
        "VERDICT: SELL\nREASONING: Bearish setup\nCONFIDENCE: 0.7"
    )

    async def _run():
        return await _run_agent(client, AGENT_ROLES[1], "TSLA", "flow")

    v = asyncio.get_event_loop().run_until_complete(_run())
    assert v.direction == "SELL"
    assert v.confidence == pytest.approx(0.7)


def test_run_agent_parses_hold():
    client = _mock_client(
        "VERDICT: HOLD\nREASONING: Unclear signal\nCONFIDENCE: 0.5"
    )

    async def _run():
        return await _run_agent(client, AGENT_ROLES[2], "SPY", "flow")

    v = asyncio.get_event_loop().run_until_complete(_run())
    assert v.direction == "HOLD"


def test_run_agent_invalid_verdict_defaults_hold():
    client = _mock_client("VERDICT: MAYBE\nREASONING: uncertain\nCONFIDENCE: 0.5")

    async def _run():
        return await _run_agent(client, AGENT_ROLES[0], "QQQ", "flow")

    v = asyncio.get_event_loop().run_until_complete(_run())
    assert v.direction == "HOLD"


def test_run_agent_bad_confidence_defaults_half():
    client = _mock_client("VERDICT: BUY\nREASONING: great\nCONFIDENCE: not_a_float")

    async def _run():
        return await _run_agent(client, AGENT_ROLES[0], "AAPL", "flow")

    v = asyncio.get_event_loop().run_until_complete(_run())
    assert v.confidence == pytest.approx(0.5)


def test_run_agent_exception_returns_hold_fallback():
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("network err"))

    async def _run():
        return await _run_agent(client, AGENT_ROLES[0], "AAPL", "flow")

    v = asyncio.get_event_loop().run_until_complete(_run())
    assert v.direction == "HOLD"
    assert "Fallback" in v.reasoning


def test_run_agent_clamps_confidence():
    client = _mock_client("VERDICT: BUY\nREASONING: good\nCONFIDENCE: 9.9")

    async def _run():
        return await _run_agent(client, AGENT_ROLES[0], "AAPL", "flow")

    v = asyncio.get_event_loop().run_until_complete(_run())
    assert v.confidence == pytest.approx(1.0)


# --- SwarmEngine.run() with live client mock ---

def test_swarm_engine_run_with_mocked_client():
    """Exercises the asyncio.gather(_run_agent...) path with a real client mock."""
    async def _run():
        with patch("simulation.swarm_engine.settings") as mock_settings:
            mock_settings.GROQ_API_KEY = "fake-key"
            mock_settings.SWARM_N_AGENTS = 3
            engine = SwarmEngine(n_agents=3)
            engine.client = _mock_client("VERDICT: BUY\nREASONING: flow\nCONFIDENCE: 0.8")
            return await engine.run("AAPL", [{"ticker": "AAPL", "premium": 500_000,
                                              "contract_type": "CALL", "strike": 180,
                                              "expiry": "2026-06-20", "sentiment": "BULLISH",
                                              "influence_tier": "WHALE", "is_golden_sweep": False}])

    verdicts = asyncio.get_event_loop().run_until_complete(_run())
    assert len(verdicts) == 3
    assert all(v.direction == "BUY" for v in verdicts)
