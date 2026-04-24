/**
 * Tests for the useFlow hook.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { useFlow } from "../src/hooks/useFlow";

// Mock api module
jest.mock("../src/lib/api", () => ({
  api: {
    getFlow: jest.fn(),
  },
}));

import { api } from "../src/lib/api";
const mockGetFlow = api.getFlow as jest.Mock;

describe("useFlow", () => {
  beforeEach(() => mockGetFlow.mockReset());

  it("starts with empty events and no error", () => {
    const { result } = renderHook(() => useFlow("test-token"));
    expect(result.current.events).toEqual([]);
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("fetches all events when ticker is empty string", async () => {
    const mockEvents = [
      { ticker: "AAPL", contract_type: "CALL", strike: 200, expiry: "2026-05-17",
        premium: 50000, trade_type: "SWEEP", sentiment: "BULLISH",
        influence_tier: "WHALE", conviction_score: 0.85, is_golden_sweep: true,
        timestamp: new Date().toISOString() },
    ];
    mockGetFlow.mockResolvedValueOnce({ events: mockEvents, total: 1, limit: 100, offset: 0 });

    const { result } = renderHook(() => useFlow("test-token"));

    await act(async () => {
      await result.current.fetch("");
    });

    expect(mockGetFlow).toHaveBeenCalledWith("", "test-token", 100, 0);
    expect(result.current.events).toEqual(mockEvents);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("fetches filtered events when ticker is provided", async () => {
    mockGetFlow.mockResolvedValueOnce({ events: [], total: 0, limit: 100, offset: 0 });

    const { result } = renderHook(() => useFlow("test-token"));

    await act(async () => {
      await result.current.fetch("TSLA");
    });

    expect(mockGetFlow).toHaveBeenCalledWith("TSLA", "test-token", 100, 0);
  });

  it("sets error state on API failure", async () => {
    mockGetFlow.mockRejectedValueOnce(new Error("Backend unreachable"));

    const { result } = renderHook(() => useFlow("test-token"));

    await act(async () => {
      await result.current.fetch("");
    });

    expect(result.current.error).toBe("Backend unreachable");
    expect(result.current.events).toEqual([]);
  });

  it("does not fetch when token is null", async () => {
    const { result } = renderHook(() => useFlow(null));

    await act(async () => {
      await result.current.fetch("AAPL");
    });

    expect(mockGetFlow).not.toHaveBeenCalled();
  });

  it("sets loading=true during fetch and false after", async () => {
    let resolvePromise!: (v: unknown) => void;
    mockGetFlow.mockReturnValueOnce(
      new Promise(r => { resolvePromise = r; })
    );

    const { result } = renderHook(() => useFlow("test-token"));

    act(() => { result.current.fetch(""); });

    await waitFor(() => expect(result.current.loading).toBe(true));

    resolvePromise({ events: [], total: 0, limit: 100, offset: 0 });
    await waitFor(() => expect(result.current.loading).toBe(false));
  });
});
