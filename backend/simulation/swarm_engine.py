"""
AI Swarm Engine: multiple LLM agents with distinct trading roles
each independently evaluate the options flow and vote on direction.
"""
import asyncio
from dataclasses import dataclass
from typing import List, Dict
from openai import AsyncOpenAI
from config import settings

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "llama-3.3-70b-versatile"

AGENT_ROLES = [
    {"role":"momentum","name":"Momentum Trader","prompt":"You are an aggressive momentum trader. You follow the tape and big money flow. Evaluate options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning."},
    {"role":"contrarian","name":"Contrarian Analyst","prompt":"You are a contrarian analyst. You look for overextension and fade crowded trades. Evaluate options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning."},
    {"role":"fundamental","name":"Fundamental Analyst","prompt":"You are a fundamental analyst. You weigh options flow against valuation and earnings catalysts. Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."},
    {"role":"technical","name":"Technical Analyst","prompt":"You are a technical analyst focused on chart patterns and IV. Evaluate options flow in the context of technical levels. Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."},
    {"role":"macro","name":"Macro Strategist","prompt":"You are a macro strategist. You assess options flow in the context of broader market conditions. Give a BUY, SELL, or HOLD verdict with one sentence of reasoning."},
    {"role":"risk","name":"Risk Manager","prompt":"You are a risk manager. You focus on downside, position sizing, and tail risk. Evaluate options flow and give a BUY, SELL, or HOLD verdict with one sentence of reasoning."},
]

@dataclass
class AgentVerdict:
    role: str
    direction: str
    reasoning: str
    confidence: float = 0.5

async def run_agent(client: AsyncOpenAI, agent_def: Dict, ticker: str, flow_summary: str) -> AgentVerdict:
    system_msg = agent_def["prompt"]
    user_msg = (
        f"Ticker: {ticker}\n"
        f"Options flow summary:\n{flow_summary}\n\n"
        "Respond in exactly this format:\n"
        "VERDICT: [BUY|SELL|HOLD]\n"
        "REASONING: one sentence\n"
        "CONFIDENCE: [0.0-1.0]"
    )
    try:
        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"system","content":system_msg},{"role":"user","content":user_msg}],
            temperature=0.2,
        )
        text = response.choices[0].message.content or ""
        direction, reasoning, confidence = "HOLD", "Insufficient data.", 0.5
        for line in text.splitlines():
            if line.upper().startswith("VERDICT:"):
                direction = line.split(":",1)[1].strip().upper()
            elif line.upper().startswith("REASONING:"):
                reasoning = line.split(":",1)[1].strip()
            elif line.upper().startswith("CONFIDENCE:"):
                try:
                    confidence = float(line.split(":",1)[1].strip())
                except Exception:
                    confidence = 0.5
        if direction not in {"BUY","SELL","HOLD"}:
            direction = "HOLD"
        return AgentVerdict(role=agent_def["role"], direction=direction, reasoning=reasoning, confidence=max(0.0, min(confidence, 1.0)))
    except Exception:
        return AgentVerdict(role=agent_def["role"], direction="HOLD", reasoning="Fallback verdict due to model unavailability.", confidence=0.5)

class SwarmEngine:
    def __init__(self, n_agents: int = 6):
        self.n_agents = max(1, min(n_agents, len(AGENT_ROLES)))
        self.client = (
            AsyncOpenAI(api_key=settings.GROQ_API_KEY, base_url=GROQ_BASE_URL)
            if settings.GROQ_API_KEY else None
        )

    async def run(self, ticker: str, flow_summary: str) -> List[AgentVerdict]:
        if not self.client:
            roles = AGENT_ROLES[:self.n_agents]
            return [AgentVerdict(role=r["role"], direction="HOLD", reasoning="No AI provider configured.", confidence=0.5) for r in roles]
        tasks = [run_agent(self.client, r, ticker, flow_summary) for r in AGENT_ROLES[:self.n_agents]]
        return await asyncio.gather(*tasks)
