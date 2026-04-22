"""
AI Swarm Engine: multiple LLM agents with distinct trading roles
each independently evaluate the options flow and vote on direction.
"""
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
from config import settings

AGENT_ROLES = [
    {
        "role":   "momentum",
        "name":   "Momentum Trader",
        "prompt": "You are an aggressive momentum trader. You follow the tape and big money flow. Evaluate options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning.",
    },
    {
        "role":   "contrarian",
        "name":   "Contrarian Analyst",
        "prompt": "You are a contrarian analyst. You look for overextension and fade crowded trades. Evaluate options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning.",
    },
    {
        "role":   "fundamental",
        "name":   "Fundamental Analyst",
        "prompt": "You are a fundamental analyst. You weigh options flow against valuation and earnings catalysts. Give a BUY, SELL, or HOLD verdict with one sentence of reasoning.",
    },
    {
        "role":   "technical",
        "name":   "Technical Analyst",
        "prompt": "You are a technical analyst focused on chart patterns and IV. Evaluate options flow in the context of technical levels. Give a BUY, SELL, or HOLD verdict with one sentence of reasoning.",
    },
    {
        "role":   "macro",
        "name":   "Macro Strategist",
        "prompt": "You are a macro strategist. You assess options flow in the context of broader market conditions. Give a BUY, SELL, or HOLD verdict with one sentence of reasoning.",
    },
    {
        "role":   "risk",
        "name":   "Risk Manager",
        "prompt": "You are a risk manager. You focus on downside, position sizing, and tail risk. Evaluate options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning.",
    },
]

@dataclass
class AgentVerdict:
    role:      str
    direction: str   # BUY | SELL | HOLD
    reasoning: str
    confidence: float = 0.5


async def run_agent(
    client:     AsyncOpenAI,
    agent_def:  Dict,
    ticker:     str,
    flow_summary: str,
) -> AgentVerdict:
    """Run a single agent and parse its verdict."""
    system_msg = agent_def["prompt"]
    user_msg = (
        f"Ticker: {ticker}
"
        f"Options flow summary:
{flow_summary}

"
        f"Respond in exactly this format:
"
        f"VERDICT: [BUY|SELL|HOLD]
"
        f"REASONING: [one sentence]"
    )
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system",  "content":system_msg},
                {"role":"user",    "content":user_msg},
            ],
            max_tokens=120,
            temperature=0.4,
        )
        text = resp.choices[0].message.content or ""
        direction = "HOLD"
        reasoning = text
        for line in text.split("
"):
            if line.startswith("VERDICT:"):
                v = line.replace("VERDICT:","").strip().upper()
                if v in ("BUY","SELL","HOLD"):
                    direction = v
            if line.startswith("REASONING:"):
                reasoning = line.replace("REASONING:","").strip()
        return AgentVerdict(role=agent_def["role"], direction=direction, reasoning=reasoning)
    except Exception as e:
        return AgentVerdict(role=agent_def["role"], direction="HOLD", reasoning=f"Error: {str(e)}")


def build_flow_summary(ticker: str, flow_events: List[Dict]) -> str:
    if not flow_events:
        return f"No recent options flow data for {ticker}."
    lines = [f"Recent options flow for {ticker}:"]
    for ev in flow_events[:10]:
        prem = ev.get("premium",0)
        prem_str = f"${prem/1_000_000:.1f}M" if prem >= 1_000_000 else f"${prem/1000:.0f}K"
        lines.append(
            f"  {ev.get('contract_type','?')} ${ev.get('strike','?')} "
            f"exp {ev.get('expiry','?')} | {ev.get('trade_type','?')} | "
            f"{prem_str} | {ev.get('sentiment','?')} | {ev.get('influence_tier','?')}"
        )
    return "
".join(lines)


class SwarmEngine:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY) if settings.OPENAI_API_KEY else None

    async def run(
        self,
        ticker:      str,
        flow_events: List[Dict],
        n_agents:    int = 6,
        n_runs:      int = 1,
        on_progress: Optional[Any] = None,
    ) -> List[AgentVerdict]:
        """Run n_agents × n_runs calls and return all verdicts."""
        if not self.client:
            # Demo mode: return mock verdicts
            import random
            rng = random.Random(hash(ticker) % 9999)
            dirs = ["BUY","BUY","BUY","SELL","HOLD"]
            return [
                AgentVerdict(
                    role      = AGENT_ROLES[i % len(AGENT_ROLES)]["role"],
                    direction = rng.choice(dirs),
                    reasoning = f"Demo mode: {AGENT_ROLES[i%len(AGENT_ROLES)]['name']} evaluating {ticker}.",
                    confidence = round(rng.uniform(0.4, 0.9), 2),
                )
                for i in range(n_agents * n_runs)
            ]

        summary  = build_flow_summary(ticker, flow_events)
        roles    = (AGENT_ROLES * n_runs)[:n_agents * n_runs]
        tasks    = [run_agent(self.client, r, ticker, summary) for r in roles]
        results  = []
        for coro in asyncio.as_completed(tasks):
            v = await coro
            results.append(v)
            if on_progress:
                on_progress(len(results) / len(tasks))
        return results
