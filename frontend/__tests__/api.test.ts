/**
 * Regression tests for src/lib/api.ts
 *
 * Covers:
 *   - req() throws a timeout error when the request takes longer than TIMEOUT_MS
 *   - req() throws an HTTP error with the backend's `detail` field when res.ok is false
 *   - req() throws a generic HTTP N error when detail is not present
 *   - req() throws non-Error fetch exceptions unchanged
 *   - api.login builds the correct URLSearchParams body
 *   - api.register sends JSON body with email+password
 *   - api.getFlow builds correct query string (ticker, limit, offset)
 *   - api.getFlow omits ticker when empty string
 *   - api.getStats sends Authorization header to /api/signals/stream/stats
 *   - api.getComposite constructs correct URL path
 *   - api.runSimulation sends correct JSON body
 *   - api.getSignalHistory builds correct query string with all params
 *   - api.getSignalHistory omits optional params when not provided
 */

// ── mock global fetch ────────────────────────────────────────────────────────
function mockFetch(status: number, body: unknown = {}) {
  global.fetch = jest.fn().mockResolvedValue({
    ok:     status >= 200 && status < 300,
    status,
    json:   () => Promise.resolve(body),
  } as Response);
}

function mockFetchError(err: Error) {
  global.fetch = jest.fn().mockRejectedValue(err);
}

// ── import after mocks set up ─────────────────────────────────────────────────
import { api } from "../src/lib/api";

const TOKEN = "test.jwt.token";

describe("api.ts", () => {
  beforeEach(() => jest.clearAllMocks());

  // ── req() error handling ──────────────────────────────────────────────────

  it("throws 'detail' from error body when res.ok is false", async () => {
    mockFetch(401, { detail: "Invalid credentials" });
    await expect(api.login("a@b.com", "pw")).rejects.toThrow("Invalid credentials");
  });

  it("throws generic 'HTTP N' when detail is absent in error body", async () => {
    mockFetch(500, {});
    await expect(api.login("a@b.com", "pw")).rejects.toThrow("HTTP 500");
  });

  it("throws AbortError as timeout message", async () => {
    const abortErr = new Error("AbortError");
    abortErr.name = "AbortError";
    mockFetchError(abortErr);
    await expect(api.getStats(TOKEN)).rejects.toThrow(/timed out/i);
  });

  it("rethrows non-AbortError fetch exceptions unchanged", async () => {
    mockFetchError(new Error("Network down"));
    await expect(api.getStats(TOKEN)).rejects.toThrow("Network down");
  });

  // ── api.login ─────────────────────────────────────────────────────────────

  it("api.login sends username+password as URLSearchParams", async () => {
    mockFetch(200, { access_token: "tok" });
    await api.login("trader@cipher.io", "pw123");
    const call = (global.fetch as jest.Mock).mock.calls[0];
    const body = call[1].body as URLSearchParams;
    expect(body.get("username")).toBe("trader@cipher.io");
    expect(body.get("password")).toBe("pw123");
    expect(call[1].headers["Content-Type"]).toBe("application/x-www-form-urlencoded");
  });

  // ── api.register ──────────────────────────────────────────────────────────

  it("api.register sends JSON body with email and password", async () => {
    mockFetch(201, { message: "created" });
    await api.register("new@cipher.io", "secret123");
    const call = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(call[1].body);
    expect(body.email).toBe("new@cipher.io");
    expect(body.password).toBe("secret123");
  });

  // ── api.getFlow ───────────────────────────────────────────────────────────

  it("api.getFlow builds correct query string with ticker", async () => {
    mockFetch(200, { ticker: "AAPL", events: [], total: 0, limit: 100, offset: 0 });
    await api.getFlow("AAPL", TOKEN);
    const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
    expect(url).toContain("ticker=AAPL");
    expect(url).toContain("limit=100");
    expect(url).toContain("offset=0");
  });

  it("api.getFlow omits ticker param when ticker is empty string", async () => {
    mockFetch(200, { ticker: null, events: [], total: 0, limit: 100, offset: 0 });
    await api.getFlow("", TOKEN);
    const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
    expect(url).not.toContain("ticker=");
  });

  it("api.getFlow sends Authorization header", async () => {
    mockFetch(200, { ticker: null, events: [], total: 0, limit: 100, offset: 0 });
    await api.getFlow("", TOKEN);
    const call = (global.fetch as jest.Mock).mock.calls[0];
    expect(call[1].headers["Authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  it("api.getFlow respects custom limit and offset", async () => {
    mockFetch(200, { ticker: null, events: [], total: 0, limit: 25, offset: 50 });
    await api.getFlow("", TOKEN, 25, 50);
    const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
    expect(url).toContain("limit=25");
    expect(url).toContain("offset=50");
  });

  // ── api.getStats ──────────────────────────────────────────────────────────

  it("api.getStats sends Authorization header to /api/signals/stream/stats", async () => {
    mockFetch(200, { stats: null });
    await api.getStats(TOKEN);
    const call = (global.fetch as jest.Mock).mock.calls[0];
    expect(call[0]).toContain("/api/signals/stream/stats");
    expect(call[1].headers["Authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  // ── api.getComposite ──────────────────────────────────────────────────────

  it("api.getComposite constructs correct ticker URL path", async () => {
    mockFetch(200, { ticker: "TSLA" });
    await api.getComposite("TSLA", TOKEN);
    const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
    expect(url).toContain("/api/signals/composite/TSLA");
  });

  // ── api.runSimulation ─────────────────────────────────────────────────────

  it("api.runSimulation sends correct JSON body", async () => {
    mockFetch(200, { ticker: "SPY", direction: "BUY" });
    await api.runSimulation("SPY", [], 6, 3, TOKEN);
    const call = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(call[1].body);
    expect(body.ticker).toBe("SPY");
    expect(body.n_agents).toBe(6);
    expect(body.n_runs).toBe(3);
    expect(body.flow_events).toEqual([]);
  });

  // ── api.getSignalHistory ──────────────────────────────────────────────────

  it("api.getSignalHistory builds full query string with all params", async () => {
    mockFetch(200, { signals: [], total: 0, limit: 50, offset: 0 });
    await api.getSignalHistory(TOKEN, {
      ticker: "AAPL", direction: "bullish", tier: "whale",
      min_conviction: 0.65, limit: 20, offset: 40,
    });
    const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
    expect(url).toContain("ticker=AAPL");
    expect(url).toContain("direction=bullish");
    expect(url).toContain("tier=whale");
    expect(url).toContain("min_conviction=0.65");
    expect(url).toContain("limit=20");
    expect(url).toContain("offset=40");
  });

  it("api.getSignalHistory omits optional params when not provided", async () => {
    mockFetch(200, { signals: [], total: 0, limit: 50, offset: 0 });
    await api.getSignalHistory(TOKEN, {});
    const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
    expect(url).not.toContain("ticker=");
    expect(url).not.toContain("direction=");
    expect(url).not.toContain("tier=");
  });
});
