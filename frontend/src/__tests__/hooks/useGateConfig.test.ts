/**
 * useGateConfig.test.ts — 100% coverage for useGateConfig hook
 * ADMIN-UI-001 | Chunk 2
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { useGateConfig } from "@/hooks/useGateConfig";
import type { GateConfigResponse } from "@/types/gates";

const MOCK_RESPONSE: GateConfigResponse = {
  epoch: 3,
  gates: [
    { gate_name: "min_premium", tier: 1, value: 10000, min_value: 1000, max_value: 500000, tier_independent: false },
    { gate_name: "min_premium", tier: 2, value: 25000, min_value: 1000, max_value: 500000, tier_independent: false },
    { gate_name: "min_premium", tier: 3, value: 50000, min_value: 1000, max_value: 500000, tier_independent: false },
  ],
};

beforeEach(() => {
  jest.useFakeTimers();
  (global.fetch as jest.Mock) = jest.fn();
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe("useGateConfig", () => {
  it("starts with loading=true and no data when token is provided", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => MOCK_RESPONSE,
    });
    const { result } = renderHook(() => useGateConfig("tok_abc"));
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
    await waitFor(() => expect(result.current.loading).toBe(false));
  });

  it("populates data on successful fetch", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => MOCK_RESPONSE,
    });
    const { result } = renderHook(() => useGateConfig("tok_abc"));
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(result.current.data?.epoch).toBe(3);
    expect(result.current.data?.gates).toHaveLength(3);
    expect(result.current.error).toBeNull();
  });

  it("sets error on 401 response", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: async () => ({}),
    });
    const { result } = renderHook(() => useGateConfig("bad_tok"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Unauthorized — admin role required.");
    expect(result.current.data).toBeNull();
  });

  it("sets error with detail from non-ok response body", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: "Internal Server Error" }),
    });
    const { result } = renderHook(() => useGateConfig("tok_abc"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Internal Server Error");
  });

  it("falls back to HTTP status string when body has no detail", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({}),
    });
    const { result } = renderHook(() => useGateConfig("tok_abc"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("HTTP 503");
  });

  it("sets error on network failure", async () => {
    (global.fetch as jest.Mock).mockRejectedValueOnce(new Error("Network error"));
    const { result } = renderHook(() => useGateConfig("tok_abc"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Network error");
  });

  it("sets generic error string for non-Error throws", async () => {
    (global.fetch as jest.Mock).mockRejectedValueOnce("oops");
    const { result } = renderHook(() => useGateConfig("tok_abc"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Network error");
  });

  it("does not fetch when token is null", () => {
    const { result } = renderHook(() => useGateConfig(null));
    expect(global.fetch).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBeNull();
  });

  it("re-fetches when token changes from null to a value", async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => MOCK_RESPONSE,
    });
    const { result, rerender } = renderHook(
      ({ tok }: { tok: string | null }) => useGateConfig(tok),
      { initialProps: { tok: null } },
    );
    expect(global.fetch).not.toHaveBeenCalled();
    rerender({ tok: "tok_new" });
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("polls every 30 seconds", async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => MOCK_RESPONSE,
    });
    renderHook(() => useGateConfig("tok_abc"));
    // Initial fetch
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    // Advance 30s → second poll
    await act(async () => { jest.advanceTimersByTime(30_000); });
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    // Advance another 30s → third poll
    await act(async () => { jest.advanceTimersByTime(30_000); });
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(3));
  });

  it("clears poll interval on unmount", async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => MOCK_RESPONSE,
    });
    const { unmount } = renderHook(() => useGateConfig("tok_abc"));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => { jest.advanceTimersByTime(60_000); });
    // Should still only be 1 call (no polling after unmount)
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("refresh() triggers a new fetch", async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => MOCK_RESPONSE,
    });
    const { result } = renderHook(() => useGateConfig("tok_abc"));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    await act(async () => { result.current.refresh(); });
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
  });

  it("sends Authorization header with token", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => MOCK_RESPONSE,
    });
    renderHook(() => useGateConfig("tok_secret"));
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/admin/gate-config",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok_secret" },
      }),
    );
  });
});
