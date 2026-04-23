/**
 * All fetch calls use RELATIVE paths (/api/...).
 *
 * In development  → Next.js dev server proxies to localhost:8000
 * On Vercel       → Next.js API route at /api/proxy/[...path] forwards to Railway
 *
 * This eliminates:
 *  - Mixed-content errors (HTTPS page fetching HTTP Railway URL)
 *  - CORS preflight failures (same-origin requests have no CORS)
 *  - "Failed to fetch" when NEXT_PUBLIC_API_URL is missing/wrong in Vercel
 */

// Default request timeout (ms). Cold Railway starts can take ~10-12s.
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
}
export interface StreamStats {
  active_symbols: number; ticks: number; classified: number;
  signals: number; errors: number;
}

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  // Always use relative /api/... so the Next.js proxy handles routing.
  // Never call the Railway URL directly from the browser.
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

  getFlow: (ticker: string, token: string) =>
    req<{ events: FlowEvent[] }>(`/api/flow/scan?ticker=${ticker}`, {
      headers: { Authorization: `Bearer ${token}` },
    }),

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
};
