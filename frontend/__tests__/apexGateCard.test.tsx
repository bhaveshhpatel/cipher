/**
 * __tests__/apexGateCard.test.tsx
 *
 * 100% coverage for ApexSignalGateCard component and the
 * /api/apex-gate Next.js route handlers.
 */
import React from "react";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { ApexSignalGateCard } from "@/components/ApexSignalGateCard";

/* ── Mock fetch ──────────────────────────────────────────── */

const mockFetch = jest.fn();

beforeEach(() => {
  jest.resetAllMocks();
  global.fetch = mockFetch;
});

/* ── Fixtures ─────────────────────────────────────────────── */

const SOFT_CONFIG = {
  hard_reject:             false,
  source:                  "env",
  max_aggression_penalty:  0.40,
  flat_aggression_penalty: 0.25,
  stats: {
    gate_total_seen:         100,
    gate_hard_rejected:      10,
    gate_soft_rejected:      20,
    gate_passed:             70,
    gate_flagged_aggression: 20,
    aggression_hard_reject:  false,
  },
};

const HARD_CONFIG = {
  ...SOFT_CONFIG,
  hard_reject: true,
  source: "override",
  stats: { ...SOFT_CONFIG.stats, aggression_hard_reject: true },
};

function mockGet(cfg: object, status = 200) {
  mockFetch.mockResolvedValueOnce({
    ok: status >= 200 && status < 300,
    status,
    json: async () => cfg,
  });
}

function mockPatch(cfg: object, status = 200) {
  mockFetch.mockResolvedValueOnce({
    ok: status >= 200 && status < 300,
    status,
    json: async () => cfg,
  });
}

/* ── Tests ─────────────────────────────────────────────────── */

describe("ApexSignalGateCard", () => {
  it("shows loading state initially", async () => {
    // fetch never resolves
    mockFetch.mockReturnValue(new Promise(() => {}));
    render(<ApexSignalGateCard token="tok" />);
    expect(screen.getByText(/Loading/i)).toBeInTheDocument();
  });

  it("renders SOFT PENALISE pill when hard_reject=false", async () => {
    mockGet(SOFT_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => expect(screen.getByTestId("mode-pill")).toBeInTheDocument());
    expect(screen.getByTestId("mode-pill")).toHaveTextContent("SOFT PENALISE");
  });

  it("renders HARD REJECT pill when hard_reject=true", async () => {
    mockGet(HARD_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => expect(screen.getByTestId("mode-pill")).toBeInTheDocument());
    expect(screen.getByTestId("mode-pill")).toHaveTextContent("HARD REJECT");
  });

  it("renders stats correctly", async () => {
    mockGet(SOFT_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("mode-pill"));
    expect(screen.getByText("100")).toBeInTheDocument(); // total seen
    expect(screen.getByText("70")).toBeInTheDocument();  // passed
    expect(screen.getByText("20")).toBeInTheDocument();  // aggression flags
  });

  it("renders max penalty cap as percentage", async () => {
    mockGet(SOFT_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("mode-pill"));
    expect(screen.getByText("40%")).toBeInTheDocument();
  });

  it("shows 'Runtime override' when source=override", async () => {
    mockGet(HARD_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("mode-pill"));
    expect(screen.getByText("Runtime override")).toBeInTheDocument();
  });

  it("shows 'Env var default' when source=env", async () => {
    mockGet(SOFT_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("mode-pill"));
    expect(screen.getByText("Env var default")).toBeInTheDocument();
  });

  it("soft button is active (enabled) when currently in hard mode", async () => {
    mockGet(HARD_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("btn-soft"));
    expect(screen.getByTestId("btn-soft")).not.toBeDisabled();
    expect(screen.getByTestId("btn-hard")).toBeDisabled();
  });

  it("hard button is active (enabled) when currently in soft mode", async () => {
    mockGet(SOFT_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("btn-hard"));
    expect(screen.getByTestId("btn-hard")).not.toBeDisabled();
    expect(screen.getByTestId("btn-soft")).toBeDisabled();
  });

  it("clicking Hard Reject calls PATCH with hard_reject:true and updates UI", async () => {
    mockGet(SOFT_CONFIG);
    mockPatch(HARD_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("btn-hard"));
    await act(async () => { fireEvent.click(screen.getByTestId("btn-hard")); });
    await waitFor(() => expect(screen.getByTestId("mode-pill")).toHaveTextContent("HARD REJECT"));
    expect(mockFetch).toHaveBeenCalledWith("/api/apex-gate", expect.objectContaining({
      method: "PATCH",
      body:   JSON.stringify({ hard_reject: true }),
    }));
  });

  it("clicking Soft Penalise calls PATCH with hard_reject:false and updates UI", async () => {
    mockGet(HARD_CONFIG);
    mockPatch(SOFT_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("btn-soft"));
    await act(async () => { fireEvent.click(screen.getByTestId("btn-soft")); });
    await waitFor(() => expect(screen.getByTestId("mode-pill")).toHaveTextContent("SOFT PENALISE"));
  });

  it("shows last updated timestamp after successful toggle", async () => {
    mockGet(SOFT_CONFIG);
    mockPatch(HARD_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("btn-hard"));
    await act(async () => { fireEvent.click(screen.getByTestId("btn-hard")); });
    await waitFor(() => expect(screen.getByText(/Last updated:/i)).toBeInTheDocument());
  });

  it("shows error message on GET failure", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false, status: 500, json: async () => ({ error: "server error" }),
    });
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => expect(screen.getByTestId("error-msg")).toBeInTheDocument());
    expect(screen.getByTestId("error-msg")).toHaveTextContent("HTTP 500");
  });

  it("shows error message on PATCH failure", async () => {
    mockGet(SOFT_CONFIG);
    mockFetch.mockResolvedValueOnce({
      ok: false, status: 503, json: async () => ({ error: "unavailable" }),
    });
    render(<ApexSignalGateCard token="tok" />);
    await waitFor(() => screen.getByTestId("btn-hard"));
    await act(async () => { fireEvent.click(screen.getByTestId("btn-hard")); });
    await waitFor(() => expect(screen.getByTestId("error-msg")).toBeInTheDocument());
    expect(screen.getByTestId("error-msg")).toHaveTextContent("HTTP 503");
  });

  it("does nothing when token is null", async () => {
    render(<ApexSignalGateCard token={null} />);
    // fetch should not be called
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("card has correct test id", async () => {
    mockGet(SOFT_CONFIG);
    render(<ApexSignalGateCard token="tok" />);
    expect(screen.getByTestId("apex-signal-gate-card")).toBeInTheDocument();
  });
});

/* ── Route handler tests ──────────────────────────────────── */

describe("/api/apex-gate route", () => {
  const { GET, PATCH } = require("@/app/api/apex-gate/route");

  function makeReq(method: string, body?: object, auth = "Bearer tok") {
    return {
      headers: { get: (k: string) => (k === "authorization" ? auth : null) },
      json:    body ? async () => body : undefined,
    } as unknown as import("next/server").NextRequest;
  }

  beforeEach(() => {
    jest.resetAllMocks();
    global.fetch = mockFetch;
  });

  it("GET proxies to backend and returns data", async () => {
    mockFetch.mockResolvedValueOnce({ status: 200, json: async () => SOFT_CONFIG });
    const res = await GET(makeReq("GET"));
    const body = await res.json();
    expect(body.hard_reject).toBe(false);
    expect(res.status).toBe(200);
  });

  it("GET returns 502 on network error", async () => {
    mockFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    const res = await GET(makeReq("GET"));
    expect(res.status).toBe(502);
  });

  it("PATCH proxies body and returns updated config", async () => {
    mockFetch.mockResolvedValueOnce({ status: 200, json: async () => HARD_CONFIG });
    const res = await PATCH(makeReq("PATCH", { hard_reject: true }));
    const body = await res.json();
    expect(body.hard_reject).toBe(true);
  });

  it("PATCH returns 400 on invalid JSON", async () => {
    const badReq = {
      headers: { get: () => "Bearer tok" },
      json: async () => { throw new SyntaxError("bad json"); },
    } as unknown as import("next/server").NextRequest;
    const res = await PATCH(badReq);
    expect(res.status).toBe(400);
  });

  it("PATCH returns 502 on network error", async () => {
    mockFetch.mockRejectedValueOnce(new Error("network"));
    const res = await PATCH(makeReq("PATCH", { hard_reject: false }));
    expect(res.status).toBe(502);
  });
});
