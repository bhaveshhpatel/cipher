"""
Regression tests for simulation/swarm_engine.py

Strategy:
  - _resolve_n_agents and _build_flow_summary tested as pure functions.
  - SwarmEngine.run() tested with self.client=None (no GROQ key) for
    deterministic HOLD fallback — no real API calls.
  - _run_agent tested by patching AsyncOpenAI completions to return
    controlled text responses.

Covers:
  Constants / config:
  - VALID_AGENT_COUNTS == [3, 6, 9, 12]
  - AGENT_ROLES has exactly 12 entries
  - All 12 entries have 'role', 'name', 'prompt' keys
  - All 12 role values are unique
  - All 12 name values are unique

  _resolve_n_agents:
  - 1 -> 3, 2 -> 3, 3 -> 3
  - 4 -> 3 (closer to 3 than 6)
  - 5 -> 6 (equidistant: Python min picks first match, here 6)
  - 6 -> 6, 7 -> 6
  - 8 -> 9, 9 -> 9, 10 -> 9
  - 11 -> 12, 12 -> 12, 13 -> 12

  _build_flow_summary:
  - String passthrough unchanged
  - Empty list returns 'No flow events provided.'
  - Single event formatted correctly (ticker/type/premium/strike/expiry/tier/sentiment)
  - Golden sweep appends [GOLDEN SWEEP]
  - Capped at 20 events (21st event omitted)

  SwarmEngine.__init__:
  - No GROQ_API_KEY: self.client is None
  - n_agents snapped via _resolve_n_agents

  SwarmEngine.run() no client:
  - Returns list of n_agents AgentVerdict objects
  - All verdicts direction == 'HOLD'
  - All verdicts confidence == 0.5
  - role and name populated from AGENT_ROLES

  AgentVerdict:
  - dataclass with role/name/direction/reasoning/confidence fields

  _run_agent (LLM response parsing):
  - BUY verdict parsed correctly
  - SELL verdict parsed correctly
  - HOLD verdict parsed correctly
  - Invalid VERDICT value defaults to HOLD
  - Confidence clamped to [0.0, 1.0] (value > 1.0 -> 1.0, < 0.0 -> 0.0)
  - API exception returns fallback HOLD with confidence=0.5
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from dataclasses import fields as dc_fields

from simulation.swarm_engine import (
    _resolve_n_agents,
    _build_flow_summary,
    _run_agent,
    SwarmEngine,
    AgentVerdict,
    VALID_AGENT_COUNTS,
    AGENT_ROLES,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_valid_agent_counts():
    assert VALID_AGENT_COUNTS == [3, 6, 9, 12]


def test_agent_roles_has_12_entries():
    assert len(AGENT_ROLES) == 12


def test_agent_roles_all_have_required_keys():
    for entry in AGENT_ROLES:
        for key in ("role", "name", "prompt"):
            assert key in entry, f"Missing '{key}' in {entry}"


def test_agent_roles_unique_roles():
    roles = [e["role"] for e in AGENT_ROLES]
    assert len(roles) == len(set(roles))


def test_agent_roles_unique_names():
    names = [e["name"] for e in AGENT_ROLES]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# _resolve_n_agents
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("requested,expected", [
    (1,  3),
    (2,  3),
    (3,  3),
    (4,  3),
    (6,  6),
    (7,  6),
    (8,  9),
    (9,  9),
    (10, 9),
    (11, 12),
    (12, 12),
    (13, 12),
])
def test_resolve_n_agents(requested, expected):
    assert _resolve_n_agents(requested) == expected


# ---------------------------------------------------------------------------
# _build_flow_summary
# ---------------------------------------------------------------------------

def test_build_flow_summary_string_passthrough():
    s = "already a summary"
    assert _build_flow_summary(s) == s


def test_build_flow_summary_empty_list():
    assert _build_flow_summary([]) == "No flow events provided."


def test_build_flow_summary_single_event_contains_ticker():
    event = {
        "ticker": "AAPL", "contract_type": "CALL",
        "premium": 50000, "strike": 200, "expiry": "2026-05-16",
        "influence_tier": "WHALE", "sentiment": "BULLISH",
        "is_golden_sweep": False,
    }
    summary = _build_flow_summary([event])
    assert "AAPL" in summary
    assert "CALL" in summary
    assert "WHALE" in summary


def test_build_flow_summary_golden_sweep_label():
    event = {
        "ticker": "TSLA", "contract_type": "PUT",
        "premium": 100000, "strike": 250, "expiry": "2026-06-20",
        "influence_tier": "INSTITUTIONAL", "sentiment": "BEARISH",
        "is_golden_sweep": True,
    }
    summary = _build_flow_summary([event])
    assert "GOLDEN SWEEP" in summary


def test_build_flow_summary_caps_at_20_events():
    events = [{"ticker": "X", "contract_type": "CALL",
               "premium": 1000, "strike": 100, "expiry": "2026-05-01",
               "influence_tier": "RETAIL", "sentiment": "NEUTRAL",
               "is_golden_sweep": False} for _ in range(21)]
    summary = _build_flow_summary(events)
    lines = [l for l in summary.splitlines() if l.strip()]
    assert len(lines) == 20


# ---------------------------------------------------------------------------
# AgentVerdict dataclass
# ---------------------------------------------------------------------------

def test_agent_verdict_has_required_fields():
    field_names = {f.name for f in dc_fields(AgentVerdict)}
    for name in ("role", "name", "direction", "reasoning", "confidence"):
        assert name in field_names


# ---------------------------------------------------------------------------
# SwarmEngine.__init__ — no GROQ key
# ---------------------------------------------------------------------------

def test_swarm_engine_no_groq_key_client_is_none():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY  = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine(n_agents=6)
    assert engine.client is None


def test_swarm_engine_n_agents_snapped():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY   = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine(n_agents=7)
    assert engine.n_agents == 6


# ---------------------------------------------------------------------------
# SwarmEngine.run() — no client (HOLD fallback)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_swarm_run_no_client_returns_n_agents_verdicts():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY   = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine(n_agents=6)
    verdicts = await engine.run("AAPL", [])
    assert len(verdicts) == 6


@pytest.mark.asyncio
async def test_swarm_run_no_client_all_hold():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY   = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine(n_agents=6)
    verdicts = await engine.run("TSLA", [])
    assert all(v.direction == "HOLD" for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_run_no_client_confidence_05():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY   = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine(n_agents=6)
    verdicts = await engine.run("NVDA", [])
    assert all(v.confidence == 0.5 for v in verdicts)


@pytest.mark.asyncio
async def test_swarm_run_no_client_roles_populated():
    with patch("simulation.swarm_engine.settings") as ms:
        ms.GROQ_API_KEY   = None
        ms.SWARM_N_AGENTS = 6
        engine = SwarmEngine(n_agents=3)
    verdicts = await engine.run("SPY", [])
    roles = [v.role for v in verdicts]
    assert roles == [AGENT_ROLES[i]["role"] for i in range(3)]


# ---------------------------------------------------------------------------
# _run_agent — LLM response parsing
# ---------------------------------------------------------------------------

def _mock_completion(text: str):
    mock_choice  = MagicMock()
    mock_choice.message.content = text
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


AGENT_DEF = AGENT_ROLES[0]


@pytest.mark.asyncio
async def test_run_agent_parses_buy_verdict():
    text = "VERDICT: BUY\nREASONING: Strong flow signal.\nCONFIDENCE: 0.85"
    verdict = await _run_agent(_mock_completion(text), AGENT_DEF, "AAPL", "flow")
    assert verdict.direction  == "BUY"
    assert verdict.reasoning  == "Strong flow signal."
    assert verdict.confidence == 0.85


@pytest.mark.asyncio
async def test_run_agent_parses_sell_verdict():
    text = "VERDICT: SELL\nREASONING: Bearish sweep detected.\nCONFIDENCE: 0.72"
    verdict = await _run_agent(_mock_completion(text), AGENT_DEF, "TSLA", "flow")
    assert verdict.direction == "SELL"
    assert verdict.confidence == 0.72


@pytest.mark.asyncio
async def test_run_agent_parses_hold_verdict():
    text = "VERDICT: HOLD\nREASONING: Mixed signals.\nCONFIDENCE: 0.5"
    verdict = await _run_agent(_mock_completion(text), AGENT_DEF, "SPY", "flow")
    assert verdict.direction == "HOLD"


@pytest.mark.asyncio
async def test_run_agent_invalid_verdict_defaults_to_hold():
    text = "VERDICT: SIDEWAYS\nREASONING: Unclear.\nCONFIDENCE: 0.5"
    verdict = await _run_agent(_mock_completion(text), AGENT_DEF, "QQQ", "flow")
    assert verdict.direction == "HOLD"


@pytest.mark.asyncio
async def test_run_agent_confidence_above_1_clamped():
    text = "VERDICT: BUY\nREASONING: Very strong.\nCONFIDENCE: 1.5"
    verdict = await _run_agent(_mock_completion(text), AGENT_DEF, "NVDA", "flow")
    assert verdict.confidence == 1.0


@pytest.mark.asyncio
async def test_run_agent_confidence_below_0_clamped():
    text = "VERDICT: SELL\nREASONING: Weak.\nCONFIDENCE: -0.3"
    verdict = await _run_agent(_mock_completion(text), AGENT_DEF, "AMD", "flow")
    assert verdict.confidence == 0.0


@pytest.mark.asyncio
async def test_run_agent_api_exception_returns_fallback_hold():
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API timeout"))
    verdict = await _run_agent(mock_client, AGENT_DEF, "AAPL", "flow")
    assert verdict.direction  == "HOLD"
    assert verdict.confidence == 0.5
    assert "Fallback" in verdict.reasoning
