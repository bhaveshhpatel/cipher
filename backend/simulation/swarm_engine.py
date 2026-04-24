"""
AI Swarm Engine — multi-agent LLM voting on options flow.

Phase 5A changes:
  - Fixed run() signature: accepts flow_events list OR pre-built summary string
  - Agent roster expanded to 12 (configurable via SWARM_N_AGENTS env var)
  - Supported counts: 3, 6, 9, 12 — snaps to nearest valid value
  - Groq llama-3.3-70b-versatile is the primary provider
  - Graceful fallback to HOLD when no API key configured
"""
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Union
from openai import AsyncOpenAI
from config import settings

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.3-70b-versatile"

VALID_AGENT_COUNTS = [3, 6, 9, 12]

AGENT_ROLES: List[Dict] = [
    # ── Tier 1 (first 6 — original roster) ─────────────────────────────────
    {
        "role": "momentum",
        "name": "Momentum Trader",
        "prompt": (
            "You are an aggressive momentum trader. You follow the tape and big money flow. "
            "Evaluate the options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "contrarian",
        "name": "Contrarian Analyst",
        "prompt": (
            "You are a contrarian analyst. You look for overextension and fade crowded trades. "
            "Evaluate the options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "fundamental",
        "name": "Fundamental Analyst",
        "prompt": (
            "You are a fundamental analyst. You weigh options flow against valuation and earnings catalysts. "
            "Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "technical",
        "name": "Technical Analyst",
        "prompt": (
            "You are a technical analyst focused on chart patterns and implied volatility. "
            "Evaluate the options flow in the context of technical levels. "
            "Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "macro",
        "name": "Macro Strategist",
        "prompt": (
            "You are a macro strategist. You assess options flow in the context of broader market conditions "
            "and macro regime. Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "risk",
        "name": "Risk Manager",
        "prompt": (
            "You are a risk manager. You focus on downside, position sizing, and tail risk. "
            "Evaluate the options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    # ── Tier 2 (agents 7-9) ─────────────────────────────────────────────────
    {
        "role": "options_flow",
        "name": "Options Flow Specialist",
        "prompt": (
            "You are an options flow specialist. You decode unusual options activity, sweep orders, "
            "and dark pool prints to identify institutional intent. "
            "Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "quant",
        "name": "Quant / Statistical Arb",
        "prompt": (
            "You are a quantitative trader focused on statistical edges and mean reversion. "
            "Evaluate the options flow data statistically and give a BUY, SELL, or HOLD verdict "
            "with one sentence of reasoning."
        ),
    },
    {
        "role": "sentiment",
        "name": "Sentiment Analyst",
        "prompt": (
            "You are a market sentiment analyst. You read crowd psychology, fear/greed signals, "
            "and retail vs institutional divergence. "
            "Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    # ── Tier 3 (agents 10-12) ────────────────────────────────────────────────
    {
        "role": "sector_rotation",
        "name": "Sector Rotation Strategist",
        "prompt": (
            "You are a sector rotation strategist. You evaluate whether options flow signals a "
            "rotation into or out of this sector/name relative to the broader market. "
            "Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "volatility",
        "name": "Volatility Trader",
        "prompt": (
            "You are a volatility trader. You assess IV rank, term structure, and skew "
            "to determine if the options flow is driven by hedging or directional conviction. "
            "Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
    {
        "role": "tape_reader",
        "name": "Dark Pool / Tape Reader",
        "prompt": (
            "You are a tape reader specializing in dark pool prints and block trades. "
            "You identify stealth accumulation or distribution from the options flow data. "
            "Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."
        ),
    },
]


def _resolve_n_agents(requested: int) -> int:
    """Snap requested count to nearest valid value in [3, 6, 9, 12]."""
    clamped = max(3, min(requested, 12))
    return min(VALID_AGENT_COUNTS, key=lambda v: abs(v - clamped))


def _build_flow_summary(flow_events: Union[list, str]) -> str:
    """Convert a list of flow event dicts into a readable summary string."""
    if isinstance(flow_events, str):
        return flow_events
    if not flow_events:
        return "No flow events provided."
    lines = []
    for i, ev in enumerate(flow_events[:20], 1):  # cap at 20 events for token budget
        ticker   = ev.get("ticker", "?")
        ctype    = ev.get("contract_type", "?")
        premium  = ev.get("premium", 0)
        strike   = ev.get("strike", "?")
        expiry   = ev.get("expiry", "?")
        sentiment = ev.get("sentiment", "NEUTRAL")
        tier     = ev.get("influence_tier", "RETAIL")
        sweep    = " [GOLDEN SWEEP]" if ev.get("is_golden_sweep") else ""
        lines.append(
            f"{i}. {ticker} {ctype} ${strike} exp {expiry} — "
            f"${premium:,.0f} premium | {tier} | {sentiment}{sweep}"
        )
    return "\n".join(lines)


@dataclass
class AgentVerdict:
    role:       str
    name:       str
    direction:  str    # BUY | SELL | HOLD
    reasoning:  str
    confidence: float  # 0.0 - 1.0


async def _run_agent(
    client:     AsyncOpenAI,
    agent_def:  Dict,
    ticker:     str,
    flow_summary: str,
) -> AgentVerdict:
    system_msg = agent_def["prompt"]
    user_msg = (
        f"Ticker: {ticker}\n"
        f"Options flow summary:\n{flow_summary}\n\n"
        "Respond in EXACTLY this format (no extra text):\n"
        "VERDICT: [BUY|SELL|HOLD]\n"
        "REASONING: one sentence\n"
        "CONFIDENCE: [0.0-1.0]"
    )
    try:
        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=120,
        )
        text      = response.choices[0].message.content or ""
        direction = "HOLD"
        reasoning = "Insufficient data."
        confidence = 0.5
        for line in text.splitlines():
            upper = line.upper()
            if upper.startswith("VERDICT:"):
                val = line.split(":", 1)[1].strip().upper()
                if val in {"BUY", "SELL", "HOLD"}:
                    direction = val
            elif upper.startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()
            elif upper.startswith("CONFIDENCE:"):
                try:
                    confidence = float(line.split(":", 1)[1].strip())
                except Exception:
                    confidence = 0.5
        return AgentVerdict(
            role=agent_def["role"],
            name=agent_def["name"],
            direction=direction,
            reasoning=reasoning,
            confidence=max(0.0, min(confidence, 1.0)),
        )
    except Exception:
        return AgentVerdict(
            role=agent_def["role"],
            name=agent_def["name"],
            direction="HOLD",
            reasoning="Fallback verdict — model unavailable.",
            confidence=0.5,
        )


class SwarmEngine:
    """
    Multi-agent LLM swarm for options flow analysis.

    n_agents: number of agents to use — snapped to nearest of [3, 6, 9, 12].
              Defaults to settings.SWARM_N_AGENTS (env var) which defaults to 6.
    """

    def __init__(self, n_agents: int | None = None):
        requested = n_agents if n_agents is not None else settings.SWARM_N_AGENTS
        self.n_agents = _resolve_n_agents(requested)
        self.client = (
            AsyncOpenAI(api_key=settings.GROQ_API_KEY, base_url=GROQ_BASE_URL)
            if settings.GROQ_API_KEY
            else None
        )

    async def run(
        self,
        ticker:      str,
        flow_events: Union[list, str],
    ) -> List[AgentVerdict]:
        """
        Run n_agents agents against the flow events for ticker.
        flow_events: list of event dicts OR a pre-built summary string.
        """
        agents_to_run = AGENT_ROLES[: self.n_agents]
        flow_summary  = _build_flow_summary(flow_events)

        if not self.client:
            return [
                AgentVerdict(
                    role=a["role"],
                    name=a["name"],
                    direction="HOLD",
                    reasoning="No AI provider configured (GROQ_API_KEY missing).",
                    confidence=0.5,
                )
                for a in agents_to_run
            ]

        tasks = [
            _run_agent(self.client, agent_def, ticker, flow_summary)
            for agent_def in agents_to_run
        ]
        return await asyncio.gather(*tasks)
