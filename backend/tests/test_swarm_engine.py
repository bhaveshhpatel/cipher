"""
Phase 3 — test_swarm_engine.py

Covers:
  - _resolve_n_agents: boundary snapping to [3, 6, 9, 12]
  - _build_flow_summary: list→string, string passthrough, empty list, 20-event cap
  - SwarmEngine.run() no-api-key path (all HOLD fallback verdicts)
  - SwarmEngine.run() agent count matches resolved n_agents
  - _run_agent() response parsing: BUY/SELL/HOLD verdict, confidence clamping
  - _run_agent() exception path → HOLD fallback
  - _run_agent() malformed CONFIDENCE → defaults to 0.5
  - _run_agent() CONFIDENCE out of range → clamped to [0,1]
  - AGENT_ROLES has exactly 12 entries
  - VALID_AGENT_COUNTS constant
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from simulation.swarm_engine import (
    _resolve_n_agents,
    _build_flow_summary,
    VALID_AGENT_COUNTS,
    AGENT_ROLES,
    AgentVerdict,
    SwarmEngine,
    _run_agent,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# _resolve_n_agents
# ---------------------------------------------------------------------------
class TestResolveNAgents:

    def test_exact_values(self):
        for v in [3, 6, 9, 12]:
            assert _resolve_n_agents(v) == v

    def test_below_minimum_snaps_to_3(self):
        assert _resolve_n_agents(0) == 3
        assert _resolve_n_agents(1) == 3
        assert _resolve_n_agents(2) == 3

    def test_above_maximum_snaps_to_12(self):
        assert _resolve_n_agents(13) == 12
        assert _resolve_n_agents(100) == 12

    def test_midpoints_snap_to_nearest(self):
        # Between 3 and 6: 4→3, 5→6 (equidistant snaps to lower in Python min)
        assert _resolve_n_agents(4) == 3
        assert _resolve_n_agents(5) in {3, 6}  # implementation-dependent tie
        assert _resolve_n_agents(7) in {6, 9}
        assert _resolve_n_agents(10) in {9, 12}
        assert _resolve_n_agents(11) == 12

    def test_returns_only_valid_values(self):
        for requested in range(0, 15):
            assert _resolve_n_agents(requested) in VALID_AGENT_COUNTS


# ---------------------------------------------------------------------------
# _build_flow_summary
# ---------------------------------------------------------------------------
class TestBuildFlowSummary:

    def test_string_passthrough(self):
        s = "Already a summary string."
        assert _build_flow_summary(s) == s

    def test_empty_list(self):
        result = _build_flow_summary([])
        assert "No flow events" in result

    def test_list_to_string(self):
        events = [
            {
                "ticker": "AAPL",
                "contract_type": "CALL",
                "strike": 200,
                "expiry": "2026-06-20",
                "premium": 500_000,
                "sentiment": "BULLISH",
                "influence_tier": "WHALE",
                "is_golden_sweep": True,
            }
        ]
        result = _build_flow_summary(events)
        assert "AAPL" in result
        assert "CALL" in result
        assert "GOLDEN SWEEP" in result
        assert "WHALE" in result

    def test_caps_at_20_events(self):
        events = [
            {
                "ticker": "SPY",
                "contract_type": "PUT",
                "strike": 500,
                "expiry": "2026-07-18",
                "premium": 100_000,
                "sentiment": "BEARISH",
                "influence_tier": "RETAIL",
                "is_golden_sweep": False,
            }
            for _ in range(25)
        ]
        result = _build_flow_summary(events)
        # Line 21 should NOT appear in output
        lines = [l for l in result.splitlines() if l.strip()]
        assert len(lines) == 20

    def test_missing_fields_do_not_raise(self):
        # Minimal event with only ticker
        result = _build_flow_summary([{"ticker": "TSLA"}])
        assert "TSLA" in result

    def test_non_golden_sweep_no_label(self):
        events = [{
            "ticker": "NVDA",
            "contract_type": "CALL",
            "strike": 900,
            "expiry": "2026-05-16",
            "premium": 200_000,
            "sentiment": "BULLISH",
            "influence_tier": "LARGE",
            "is_golden_sweep": False,
        }]
        result = _build_flow_summary(events)
        assert "GOLDEN SWEEP" not in result


# ---------------------------------------------------------------------------
# AGENT_ROLES and VALID_AGENT_COUNTS constants
# ---------------------------------------------------------------------------
class TestConstants:

    def test_exactly_12_agent_roles(self):
        assert len(AGENT_ROLES) == 12

    def test_all_agent_roles_have_required_keys(self):
        for agent in AGENT_ROLES:
            assert "role" in agent
            assert "name" in agent
            assert "prompt" in agent

    def test_valid_agent_counts(self):
        assert VALID_AGENT_COUNTS == [3, 6, 9, 12]

    def test_agent_roles_unique_names(self):
        names = [a["name"] for a in AGENT_ROLES]
        assert len(names) == len(set(names))

    def test_agent_roles_unique_roles(self):
        roles = [a["role"] for a in AGENT_ROLES]
        assert len(roles) == len(set(roles))


# ---------------------------------------------------------------------------
# SwarmEngine no-key fallback
# ---------------------------------------------------------------------------
class TestSwarmEngineNoKey:

    def test_no_api_key_returns_all_hold(self):
        with patch("simulation.swarm_engine.settings") as mock_settings:
            mock_settings.GROQ_API_KEY = ""
            mock_settings.SWARM_N_AGENTS = 6
            engine = SwarmEngine(n_agents=6)
            verdicts = _run(engine.run("AAPL", []))
        assert len(verdicts) == 6
        for v in verdicts:
            assert v.direction == "HOLD"
            assert "GROQ_API_KEY" in v.reasoning or "No AI" in v.reasoning

    def test_no_key_count_matches_n_agents(self):
        with patch("simulation.swarm_engine.settings") as mock_settings:
            mock_settings.GROQ_API_KEY = ""
            mock_settings.SWARM_N_AGENTS = 3
            engine = SwarmEngine(n_agents=3)
            verdicts = _run(engine.run("SPY", []))
        assert len(verdicts) == 3

    def test_no_key_verdicts_have_role_and_name(self):
        with patch("simulation.swarm_engine.settings") as mock_settings:
            mock_settings.GROQ_API_KEY = ""
            mock_settings.SWARM_N_AGENTS = 6
            engine = SwarmEngine(n_agents=6)
            verdicts = _run(engine.run("QQQ", "test summary"))
        for v in verdicts:
            assert v.role
            assert v.name
            assert isinstance(v.confidence, float)


# ---------------------------------------------------------------------------
# _run_agent response parsing
# ---------------------------------------------------------------------------
class TestRunAgentParsing:

    def _make_client(self, response_text: str):
        mock_response = MagicMock()
        mock_response.choices[0].message.content = response_text
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        return mock_client

    def _agent_def(self):
        return {"role": "momentum", "name": "Momentum Trader", "prompt": "You are a momentum trader."}

    def test_parses_buy_verdict(self):
        client = self._make_client("VERDICT: BUY\nREASONING: Strong tape.\nCONFIDENCE: 0.85")
        verdict = _run(_run_agent(client, self._agent_def(), "AAPL", "flow summary"))
        assert verdict.direction == "BUY"
        assert verdict.reasoning == "Strong tape."
        assert abs(verdict.confidence - 0.85) < 0.001

    def test_parses_sell_verdict(self):
        client = self._make_client("VERDICT: SELL\nREASONING: Fading.\nCONFIDENCE: 0.7")
        verdict = _run(_run_agent(client, self._agent_def(), "SPY", "flow summary"))
        assert verdict.direction == "SELL"

    def test_parses_hold_verdict(self):
        client = self._make_client("VERDICT: HOLD\nREASONING: Unclear.\nCONFIDENCE: 0.5")
        verdict = _run(_run_agent(client, self._agent_def(), "TSLA", "flow summary"))
        assert verdict.direction == "HOLD"

    def test_invalid_verdict_defaults_to_hold(self):
        client = self._make_client("VERDICT: MAYBE\nREASONING: Who knows.\nCONFIDENCE: 0.5")
        verdict = _run(_run_agent(client, self._agent_def(), "NVDA", "flow summary"))
        assert verdict.direction == "HOLD"

    def test_malformed_confidence_defaults_to_0_5(self):
        client = self._make_client("VERDICT: BUY\nREASONING: Tape strong.\nCONFIDENCE: not_a_number")
        verdict = _run(_run_agent(client, self._agent_def(), "AAPL", "flow summary"))
        assert verdict.confidence == 0.5

    def test_confidence_clamped_above_1(self):
        client = self._make_client("VERDICT: BUY\nREASONING: Very strong.\nCONFIDENCE: 1.5")
        verdict = _run(_run_agent(client, self._agent_def(), "AAPL", "flow summary"))
        assert verdict.confidence <= 1.0

    def test_confidence_clamped_below_0(self):
        client = self._make_client("VERDICT: SELL\nREASONING: Weak.\nCONFIDENCE: -0.3")
        verdict = _run(_run_agent(client, self._agent_def(), "AAPL", "flow summary"))
        assert verdict.confidence >= 0.0

    def test_api_exception_returns_hold_fallback(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Network error"))
        verdict = _run(_run_agent(mock_client, self._agent_def(), "AAPL", "flow summary"))
        assert verdict.direction == "HOLD"
        assert "Fallback" in verdict.reasoning or "unavailable" in verdict.reasoning

    def test_role_and_name_preserved_in_fallback(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("Timeout"))
        agent_def = {"role": "contrarian", "name": "Contrarian Analyst", "prompt": "..."}
        verdict = _run(_run_agent(mock_client, agent_def, "AAPL", "flow summary"))
        assert verdict.role == "contrarian"
        assert verdict.name == "Contrarian Analyst"
