/**
 * useGatePatch.test.ts — 100% coverage for useGatePatch hook
 * ADMIN-UI-001 | Chunk 2
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { useGatePatch } from "@/hooks/useGatePatch";
import type { PatchGatePayload, PatchGateResponse } from "@/types/gates";

const PAYLOAD: PatchGatePayload = {
  gate_name:            "min_premium",
  tier:                 1,
  value:                15000,
  reason:               "raising floor",
  confirm_market_hours: false,
};

const PATCH_RESPONSE: PatchGateResponse = {
  gate_name: "min_premium",
  tier:      1,
  new_value: 15000,
  epoch:     4,
};

beforeEach(() => {
  jest.useFakeTimers();
  (global.fetch as jest.Mock) = jest.fn();
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe("useGatePatch", () => {
  it("starts with empty statusMap", () => {
    const { result } = renderHook(() => useGatePatch());
    expect(result.current.statusMap).toEqual({});
  });

  it("sets status to 'saving' then 'saved' on success", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => PATCH_RESPONSE,
    });
    const { result } = renderHook(() => useGatePatch());
    let patchResult: PatchGateResponse | null = null;
    await act(async () => {
      patchResult = await result.current.patch("tok_abc", PAYLOAD);
    });
    expect(patchResult).toEqual(PATCH_RESPONSE);
    expect(result.current.statusMap["min_premium:1"]).toBe("saved");
  });

  it("status auto-resets to 'idle' after 2500 ms", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => PATCH_RESPONSE,
    });
    const { result } = renderHook(() => useGatePatch());
    await act(async () => { await result.current.patch("tok_abc", PAYLOAD); });
    expect(result.current.statusMap["min_premium:1"]).toBe("saved");
    await act(async () => { jest.advanceTimersByTime(2_500); });
    expect(result.current.statusMap["min_premium:1"]).toBe("idle");
  });

  it("sets status to 'error' on non-ok response and throws", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: async () => ({ detail: "Value out of range" }),
    });
    const { result } = renderHook(() => useGatePatch());
    await act(async () => {
      await expect(result.current.patch("tok_abc", PAYLOAD))
        .rejects.toThrow("Value out of range");
    });
    expect(result.current.statusMap["min_premium:1"]).toBe("error");
  });

  it("falls back to HTTP status string when no detail in error body", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 422,
      json: async () => ({}),
    });
    const { result } = renderHook(() => useGatePatch());
    await act(async () => {
      await expect(result.current.patch("tok_abc", PAYLOAD))
        .rejects.toThrow("HTTP 422");
    });
    expect(result.current.statusMap["min_premium:1"]).toBe("error");
  });

  it("sets status to 'error' on network failure and re-throws", async () => {
    (global.fetch as jest.Mock).mockRejectedValueOnce(new Error("fetch failed"));
    const { result } = renderHook(() => useGatePatch());
    await act(async () => {
      await expect(result.current.patch("tok_abc", PAYLOAD))
        .rejects.toThrow("fetch failed");
    });
    expect(result.current.statusMap["min_premium:1"]).toBe("error");
  });

  it("keys status by gate_name:tier so multiple patches are independent", async () => {
    const payload2: PatchGatePayload = { ...PAYLOAD, tier: 2 };
    const response2: PatchGateResponse = { ...PATCH_RESPONSE, tier: 2 };
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => PATCH_RESPONSE })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => response2 });

    const { result } = renderHook(() => useGatePatch());
    await act(async () => {
      await result.current.patch("tok_abc", PAYLOAD);
      await result.current.patch("tok_abc", payload2);
    });
    expect(result.current.statusMap["min_premium:1"]).toBe("saved");
    expect(result.current.statusMap["min_premium:2"]).toBe("saved");
  });

  it("sends correct fetch options (method, headers, body)", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => PATCH_RESPONSE,
    });
    const { result } = renderHook(() => useGatePatch());
    await act(async () => { await result.current.patch("tok_abc", PAYLOAD); });
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/admin/gate-config",
      expect.objectContaining({
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization:  "Bearer tok_abc",
        },
        body: JSON.stringify(PAYLOAD),
      }),
    );
  });

  it("handles tier=3 key correctly", async () => {
    const p: PatchGatePayload = { ...PAYLOAD, tier: 3 };
    const r: PatchGateResponse = { ...PATCH_RESPONSE, tier: 3 };
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true, status: 200, json: async () => r,
    });
    const { result } = renderHook(() => useGatePatch());
    await act(async () => { await result.current.patch("tok_abc", p); });
    expect(result.current.statusMap["min_premium:3"]).toBe("saved");
  });

  it("handles null reason in payload", async () => {
    const p: PatchGatePayload = { ...PAYLOAD, reason: null };
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true, status: 200, json: async () => PATCH_RESPONSE,
    });
    const { result } = renderHook(() => useGatePatch());
    await act(async () => { await result.current.patch("tok_abc", p); });
    expect(result.current.statusMap["min_premium:1"]).toBe("saved");
    const callBody = JSON.parse((global.fetch as jest.Mock).mock.calls[0][1].body);
    expect(callBody.reason).toBeNull();
  });

  it("does not auto-reset error status — error stays until next patch", async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false, status: 500, json: async () => ({}),
    });
    const { result } = renderHook(() => useGatePatch());
    await act(async () => {
      try { await result.current.patch("tok_abc", PAYLOAD); } catch { /* expected */ }
    });
    await act(async () => { jest.advanceTimersByTime(5_000); });
    expect(result.current.statusMap["min_premium:1"]).toBe("error");
  });
});
