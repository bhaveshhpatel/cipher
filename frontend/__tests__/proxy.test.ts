/**
 * Tests for the Next.js proxy route handler.
 * Run with: npx jest __tests__/proxy.test.ts
 *
 * @jest-environment node
 */

const MOCK_BACKEND = "https://mock-railway.up.railway.app";

// Mock fetch globally
const mockFetch = jest.fn();
global.fetch = mockFetch;

// Mock env
process.env.BACKEND_URL = MOCK_BACKEND;

import { NextRequest } from "next/server";

// Helper to build a mock NextRequest
function makeReq(
  method: string,
  path: string,
  body?: string,
  headers?: Record<string, string>
): NextRequest {
  const url = `https://cipher.vercel.app/api/proxy/${path}`;
  return new NextRequest(url, {
    method,
    body,
    headers: { "content-type": "application/json", ...headers },
  });
}

describe("Proxy route handler", () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  it("forwards GET /flow/scan to backend correctly", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 200,
      text: async () => JSON.stringify({ events: [], total: 0, limit: 50, offset: 0 }),
      headers: { get: () => "application/json" },
    });

    // Dynamically import after env is set
    const { GET } = await import("../src/app/api/proxy/[...path]/route");
    const req = makeReq("GET", "flow/scan?ticker=AAPL");
    const res = await GET(req, { params: Promise.resolve({ path: ["flow", "scan"] }) });

    expect(mockFetch).toHaveBeenCalledWith(
      `${MOCK_BACKEND}/api/flow/scan?ticker=AAPL`,
      expect.objectContaining({ method: "GET" })
    );
    expect(res.status).toBe(200);
  });

  it("returns 503 when backend is unreachable", async () => {
    mockFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));

    const { GET } = await import("../src/app/api/proxy/[...path]/route");
    const req = makeReq("GET", "flow/scan");
    const res = await GET(req, { params: Promise.resolve({ path: ["flow", "scan"] }) });

    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.detail).toContain("unreachable");
  });

  it("forwards Authorization header from client", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 200,
      text: async () => "{}",
      headers: { get: () => "application/json" },
    });

    const { GET } = await import("../src/app/api/proxy/[...path]/route");
    const req = makeReq("GET", "flow/scan", undefined, {
      authorization: "Bearer test-token-abc",
    });
    await GET(req, { params: Promise.resolve({ path: ["flow", "scan"] }) });

    const [, fetchOpts] = mockFetch.mock.calls[0];
    expect(fetchOpts.headers["authorization"]).toBe("Bearer test-token-abc");
  });

  it("strips host header before forwarding", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 200,
      text: async () => "{}",
      headers: { get: () => "application/json" },
    });

    const { GET } = await import("../src/app/api/proxy/[...path]/route");
    const req = makeReq("GET", "flow/scan");
    await GET(req, { params: Promise.resolve({ path: ["flow", "scan"] }) });

    const [, fetchOpts] = mockFetch.mock.calls[0];
    expect(fetchOpts.headers["host"]).toBeUndefined();
  });

  it("forwards POST body as text to backend", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 200,
      text: async () => "{}",
      headers: { get: () => "application/json" },
    });

    const { POST } = await import("../src/app/api/proxy/[...path]/route");
    const bodyStr = JSON.stringify({ ticker: "TSLA", n_agents: 6 });
    const req = makeReq("POST", "simulation/run", bodyStr);
    await POST(req, { params: Promise.resolve({ path: ["simulation", "run"] }) });

    const [, fetchOpts] = mockFetch.mock.calls[0];
    expect(fetchOpts.body).toBe(bodyStr);
    expect(fetchOpts.method).toBe("POST");
  });

  it("joins multi-segment path arrays correctly", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 200,
      text: async () => "{}",
      headers: { get: () => "application/json" },
    });

    const { GET } = await import("../src/app/api/proxy/[...path]/route");
    const req = makeReq("GET", "signals/composite/SPY");
    await GET(req, { params: Promise.resolve({ path: ["signals", "composite", "SPY"] }) });

    expect(mockFetch).toHaveBeenCalledWith(
      `${MOCK_BACKEND}/api/signals/composite/SPY`,
      expect.anything()
    );
  });
});
