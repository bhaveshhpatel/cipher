"""
Regression tests for simulation/swarm_engine.py

Covers (matched to actual source):
  _resolve_n_agents:
  - Valid counts 3, 6, 9, 12 returned unchanged
  - 1 → snaps to 3 (nearest valid below 3)
  - 4 → snaps to 3 (nearest, since |4-3|=1 < |4-6|=2)
  - 5 → snaps to 6 (nearest, since |5-6|=1 < |5-3|=2)
  - 7 → snaps to 6 (nearest)
  - 8 → snaps to 9 (nearest)
  - 10 → snaps to 9 (nearest)
  - 11 → snaps to 12 (nearest)
  - 100 → clamped+snapped to 12
  - 0 → clamped to 3 then snapped to 3
  - -5 → clamped to 3

  _build_flow_summary:
  - str input returned as-is
  - empty list → 'No flow events provided.'
  - list with 1 event → single line output
  - golden sweep event → '[GOLDEN SWEEP]' appears in output
  - list capped at 20 events (21st event not in output)
  - missing fields in event handled gracefully (no KeyError)

  SwarmEngine:
  - No GROQ_API_KEY → client is None
  - No GROQ_API_KEY → run() returns all HOLD AgentVerdicts
  - With mocked client → run() returns len(n_agents) verdicts
  - Mocked BUY response → AgentVerdict.direction == 'BUY'
  - Mocked SELL response → AgentVerdict.direction == 'SELL'
  - Mocked malformed response → gracefully defaults to HOLD + confidence 0.5
  - confidence clamped to [0, 1] (no out-of-range values)
  - AgentVerdict has role, name, direction, reasoning, confidence fields
  - n_agents=12 runs 12 agents
  - n_agents=3 runs only 3 agents (not all 12)
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from simulation.swarm_engine import (
    _resolve_n_agents,
    _build_flow_summary,
    SwarmEngine,
    AgentVerdict,
    VALID_AGENT_COUNTS,
)


# ── _resolve_n_agents ────────────────────────────────────────────────────────

@pytest.mark.parametrize("n,expected", [
    (3,  3),
    (6,  6),
    (9,  9),
    (12, 12),
])
def test_resolve_n_agents_valid_counts_unchanged(n, expected):
    assert _resolve_n_agents(n) == expected


@pytest.mark.parametrize("n,expected", [
    (1,   3),   # below min → clamp to 3
    (0,   3),   # zero → clamp to 3
    (-5,  3),   # negative → clamp to 3
    (4,   3),   # nearest(4) = 3 (|4-3|=1 < |4-6|=2)
    (5,   6),   # nearest(5) = 6 (|5-6|=1 < |5-3|=2)
    (7,   6),   # nearest(7) = 6 (|7-6|=1 < |7-9|=2)
    (8,   9),   # nearest(8) = 9 (|8-9|=1 < |8-6|=2)
    (10,  9),   # nearest(10) = 9 (|10-9|=1 < |10-12|=2)
    (11, 12),   # nearest(11) = 12 (|11-12|=1 < |11-9|=2)
    (100, 12),  # above max → clamp to 12
])
def test_resolve_n_agents_snapping(n, expected):
    assert _resolve_n_agents(n) == expected, (
        f"_resolve_n_agents({n}) should be {expected}, got {_resolve_n_agents(n)}"
    )


# ── _build_flow_summary ──────────────────────────────────────────────────────

def test_build_flow_summary_string_returned_as_is():
    s = "Pre-built summary string."
    assert _build_flow_summary(s) == s


def test_build_flow_summary_empty_list():
    result = _build_flow_summary([])
    assert result == "No flow events provided."


def test_build_flow_summary_single_event_produces_one_line():
    events = [{
        "ticker": "AAPL", "contract_type": "CALL", "premium": 500_000,
        "strike": 200, "expiry": "2026-06-20",
        "sentiment": "BULLISH", "influence_tier": "WHALE", "is_golden_sweep": False,
    }]
    result = _build_flow_summary(events)
    lines = [l for l in result.splitlines() if l.strip()]
    assert len(lines) == 1


def test_build_flow_summary_golden_sweep_label_present():
    events = [{
        "ticker": "SPY", "contract_type": "CALL", "premium": 1_000_000,
        "strike": 550, "expiry": "2026-06-20",
        "sentiment": "BULLISH", "influence_tier": "WHALE", "is_golden_sweep": True,
    }]
    result = _build_flow_summary(events)
    assert "GOLDEN SWEEP" in result


def test_build_flow_summary_no_golden_sweep_label_when_false():
    events = [{
        "ticker": "SPY", "contract_type": "PUT", "premium": 200_000,
        "strike": 500, "expiry": "2026-06-20",
        "sentiment": "BEARISH", "influence_tier": "INSTITUTIONAL", "is_golden_sweep": False,
    }]
    result = _build_flow_summary(events)
    assert "GOLDEN SWEEP" not in result


def test_build_flow_summary_capped_at_20_events():
    events = [
        {"ticker": f"T{i}", "contract_type": "CALL", "premium": 100_000,
         "strike": 100, "expiry": "2026-06-20", "sentiment": "NEUTRAL",
         "influence_tier": "RETAIL", "is_golden_sweep": False}
        for i in range(25)
    ]
    result = _build_flow_summary(events)
    lines = [l for l in result.splitlines() if l.strip()]
    assert len(lines) == 20


def test_build_flow_summary_missing_fields_no_error():
    """Events with missing fields must not raise KeyError."""
    events = [{"ticker": "AAPL"}]  # missing everything else
    try:
        result = _build_flow_summary(events)
        assert isinstance(result, str)
    except KeyError as e:
        pytest.fail(f"_build_flow_summary raised KeyError on missing field: {e}")


def test_build_flow_summary_ticker_appears_in_output():
    events = [{
        "ticker": "NVDA", "contract_type": "CALL", "premium": 750_000,
        "strike": 900, "expiry": "2026-06-20",
        "sentiment": "BULLISH", "influence_tier": "WHALE", "is_golden_sweep": False,
    }]
    result = _build_flow_summary(events)
    assert "NVDA" in result


# ── SwarmEngine: no API key ──────────────────────────────────────────────────

def test_swarm_engine_no_api_key_client_is_none():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine()
    assert engine.client is None


@pytest.mark.asyncio
async def test_swarm_engine_no_api_key_returns_hold_verdicts():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine()
        verdicts = await engine.run("AAPL", [])
    assert len(verdicts) == 6
    assert all(v.direction == "HOLD" for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_engine_no_api_key_verdict_reasoning_mentions_missing_key():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine()
        verdicts = await engine.run("TSLA", [])
    assert any("GROQ_API_KEY" in v.reasoning or "unavailable" in v.reasoning.lower()
               for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_engine_no_api_key_confidence_is_05():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = None
        ms.SWARM_N_AGENTS = 3
        engine = SwarmEngine()
        verdicts = await engine.run("SPY", [])
    assert all(v.confidence == 0.5 for v in verdicts)


# ── SwarmEngine: with mocked client ─────────────────────────────────────────

def _make_mock_response(direction: str, confidence: float = 0.85) -> MagicMock:
    """Build a mock OpenAI chat completion response."""
    msg = MagicMock()
    msg.content = (
        f"VERDICT: {direction}\n"
        f"REASONING: Test reasoning for {direction}.\n"
        f"CONFIDENCE: {confidence}"
    )
    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response


@pytest.mark.asyncio
async def test_swarm_engine_mocked_client_returns_correct_count():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_mock_response("BUY")
    )
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = "fake-key"
        ms.SWARM_N_AGENTS = 6
        with patch("simulation.swarm_engine.AsyncOpenAI", return_value=mock_client):
            engine = SwarmEngine(n_agents=6)
            engine.client = mock_client
            verdicts = await engine.run("AAPL", "Some flow summary.")
    assert len(verdicts) == 6


@pytest.mark.asyncio
async def test_swarm_engine_mocked_buy_response():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_mock_response("BUY", confidence=0.9)
    )
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = "fake-key"
        ms.SWARM_N_AGENTS = 3
        with patch("simulation.swarm_engine.AsyncOpenAI", return_value=mock_client):
            engine = SwarmEngine(n_agents=3)
            engine.client = mock_client
            verdicts = await engine.run("AAPL", "Bullish flow.")
    assert all(v.direction == "BUY" for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_engine_mocked_sell_response():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_mock_response("SELL", confidence=0.7)
    )
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = "fake-key"
        ms.SWARM_N_AGENTS = 3
        with patch("simulation.swarm_engine.AsyncOpenAI", return_value=mock_client):
            engine = SwarmEngine(n_agents=3)
            engine.client = mock_client
            verdicts = await engine.run("QQQ", "Bearish flow.")
    assert all(v.direction == "SELL" for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_engine_malformed_response_defaults_to_hold():
    """If LLM returns junk (no VERDICT: line), gracefully defaults to HOLD."""
    bad_msg = MagicMock()
    bad_msg.content = "I cannot determine the verdict at this time."
    bad_choice = MagicMock()
    bad_choice.message = bad_msg
    bad_response = MagicMock()
    bad_response.choices = [bad_choice]

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=bad_response)

    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = "fake-key"
        ms.SWARM_N_AGENTS = 3
        with patch("simulation.swarm_engine.AsyncOpenAI", return_value=mock_client):
            engine = SwarmEngine(n_agents=3)
            engine.client = mock_client
            verdicts = await engine.run("AAPL", "Summary.")
    assert all(v.direction == "HOLD" for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_engine_confidence_clamped_to_0_to_1():
    """Confidence returned by LLM must be clamped to [0.0, 1.0]."""
    # LLM returns out-of-range confidence value
    msg = MagicMock()
    msg.content = "VERDICT: BUY\nREASONING: Test.\nCONFIDENCE: 99.5"
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)

    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = "fake-key"
        ms.SWARM_N_AGENTS = 3
        with patch("simulation.swarm_engine.AsyncOpenAI", return_value=mock_client):
            engine = SwarmEngine(n_agents=3)
            engine.client = mock_client
            verdicts = await engine.run("AAPL", "Summary.")
    assert all(0.0 <= v.confidence <= 1.0 for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_engine_exception_in_agent_returns_fallback_hold():
    """If OpenAI call raises an exception, agent falls back to HOLD."""
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("network error"))

    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = "fake-key"
        ms.SWARM_N_AGENTS = 3
        with patch("simulation.swarm_engine.AsyncOpenAI", return_value=mock_client):
            engine = SwarmEngine(n_agents=3)
            engine.client = mock_client
            verdicts = await engine.run("SPY", "Summary.")
    assert all(v.direction == "HOLD" for v in verdicts)
    assert all("unavailable" in v.reasoning.lower() for v in verdicts)


# ── AgentVerdict shape ───────────────────────────────────────────────────────

def test_agent_verdict_has_all_fields():
    v = AgentVerdict(
        role="momentum", name="Momentum Trader",
        direction="BUY", reasoning="Strong tape.", confidence=0.85
    )
    assert v.role == "momentum"
    assert v.name == "Momentum Trader"
    assert v.direction == "BUY"
    assert v.reasoning == "Strong tape."
    assert v.confidence == 0.85


# ── n_agents slicing ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_swarm_engine_n_agents_3_runs_only_3_agents():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = None
        ms.SWARM_N_AGENTS = 3
        engine = SwarmEngine(n_agents=3)
        verdicts = await engine.run("AAPL", [])
    assert len(verdicts) == 3


@pytest.mark.asyncio
async def test_swarm_engine_n_agents_12_runs_12_agents():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY = None
        ms.SWARM_N_AGENTS = 12
        engine = SwarmEngine(n_agents=12)
        verdicts = await engine.run("SPY", [])
    assert len(verdicts) == 12
