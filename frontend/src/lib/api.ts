/**
 * All fetch calls use RELATIVE paths (/api/...).
 *
 * In development  → Next.js dev server proxies to localhost:8000
 * On Vercel       → Next.js API route at /api/proxy/[...path] forwards to Railway
 */

const TIMEOUT_MS = 20_000;

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
  volume_premium_factor?: number;
}
export interface StreamStats {
  active_symbols: number; ticks: number; classified: number;
  signals: number; errors: number;
}
export interface SignalHistoryItem {
  id: number;
  ticker: string;
  recommendation: string;
  composite_score: number;
  flow_score: number;
  backtest_score: number;
  volume_premium_factor: number;
  reasoning: string | null;
  contract_type: string | null;
  direction: string | null;
  influence_tier: string | null;
  total_premium: number | null;
  trade_count: number | null;
  is_accelerating: boolean;
  signal_ts: string | null;
  created_at: string;
}
export interface SignalHistoryResponse {
  signals: SignalHistoryItem[];
  total: number;
  limit: number;
  offset: number;
}

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const url = path.startsWith("/") ? path : `/${path}`;
  try {
    const res = await fetch(url, { ...opts, signal: controller.signal });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      throw new Error((b as { detail?: string }).detail || `HTTP ${res.status}`);
    }
    return res.json() as Promise<T>;
  } catch (err: unknown) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new Error("Request timed out — server may be starting up, please retry.");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  login: (email: string, password: string) =>
    req<{ access_token: string }>("/api/auth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: email, password }),
    }),

  register: (email: string, password: string) =>
    req<{ message: string }>("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),

  // ticker = "" → backend returns all (no WHERE ticker = ... clause)
  getFlow: (ticker: string, token: string, limit = 100, offset = 0) => {
    const qs = new URLSearchParams();
    if (ticker) qs.set("ticker", ticker);
    qs.set("limit",  String(limit));
    qs.set("offset", String(offset));
    return req<{ ticker: string | null; events: FlowEvent[]; total: number; limit: number; offset: number }>(
      `/api/flow/scan?${qs.toString()}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
  },

  runSimulation: (ticker: string, events: FlowEvent[], nAgents: number, nRuns: number, token: string) =>
    req<SimulationResult>("/api/simulation/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ ticker, flow_events: events, n_agents: nAgents, n_runs: nRuns }),
    }),

  getComposite: (ticker: string, token: string) =>
    req<CompositeSignal>(`/api/signals/composite/${ticker}`, {
      headers: { Authorization: `Bearer ${token}` },
    }),

  getStats: (token: string) =>
    req<{ stats: StreamStats }>("/api/stream/stats", {
      headers: { Authorization: `Bearer ${token}` },
    }),

  getSignalHistory: (
    token: string,
    params: {
      ticker?:         string;
      direction?:      string;
      tier?:           string;
      min_conviction?: number;
      limit?:          number;
      offset?:         number;
    } = {},
  ) => {
    const qs = new URLSearchParams();
    if (params.ticker)                        qs.set("ticker",         params.ticker);
    if (params.direction)                     qs.set("direction",      params.direction);
    if (params.tier)                          qs.set("tier",           params.tier);
    if (params.min_conviction !== undefined)  qs.set("min_conviction", String(params.min_conviction));
    if (params.limit          !== undefined)  qs.set("limit",          String(params.limit));
    if (params.offset         !== undefined)  qs.set("offset",         String(params.offset));
    return req<SignalHistoryResponse>(`/api/signals/history?${qs.toString()}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  },
};

/**
 * Named alias used by login/page.tsx and its test mock.
 * The test does: jest.mock('@/lib/api', () => ({ authAPI: { login: mockLogin } }))
 * so we export `authAPI` as an alias for `api`.
 */
export const authAPI = api;
