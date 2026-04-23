// Strip any trailing slash so paths like /api/auth/register never become //api/auth/register
const API = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/+$/, "");

export interface FlowEvent {
  ticker: string; contract_type: string; strike: number; expiry: string;
  premium: number; trade_type: string; sentiment: string; influence_tier: string;
  conviction_score: number; is_golden_sweep: boolean; timestamp: string;
}
export interface SimulationResult {
  ticker: string; direction: string; confidence: number;
  bull_votes: number; bear_votes: number; hold_votes: number;
  summary: string;
  agents: { role: string; direction: string; reasoning: string }[];
}
export interface CompositeSignal {
  ticker: string; recommendation: string; composite_score: number;
  flow_score: number; backtest_score: number; reasoning: string;
}
export interface StreamStats {
  active_symbols: number; ticks: number; classified: number;
  signals: number; errors: number;
}

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, opts);
  if (!res.ok) { const b = await res.json().catch(()=>({})); throw new Error(b.detail||`HTTP ${res.status}`); }
  return res.json();
}

export const api = {
  login: (email: string, password: string) =>
    req<{ access_token: string }>("/api/auth/token", {
      method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"},
      body: new URLSearchParams({ username: email, password }),
    }),
  register: (email: string, password: string) =>
    req<{ message: string }>("/api/auth/register", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ email, password }),
    }),
  getFlow: (ticker: string, token: string) =>
    req<{ events: FlowEvent[] }>(`/api/flow/scan?ticker=${ticker}`, {
      headers:{ Authorization:`Bearer ${token}` },
    }),
  runSimulation: (ticker: string, events: FlowEvent[], nAgents: number, nRuns: number, token: string) =>
    req<SimulationResult>("/api/simulation/run", {
      method:"POST", headers:{"Content-Type":"application/json", Authorization:`Bearer ${token}`},
      body: JSON.stringify({ ticker, flow_events: events, n_agents: nAgents, n_runs: nRuns }),
    }),
  getComposite: (ticker: string, token: string) =>
    req<CompositeSignal>(`/api/signals/composite/${ticker}`, {
      headers:{ Authorization:`Bearer ${token}` },
    }),
  getStats: (token: string) =>
    req<{ stats: StreamStats }>("/api/stream/stats", {
      headers:{ Authorization:`Bearer ${token}` },
    }),
};
