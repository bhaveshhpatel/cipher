import React from 'react';
import { renderHook, waitFor, act } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useAlerts } from "@/hooks";
import type { Alert } from "@/types";

const Wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(SWRConfig, {
    value: { provider: () => new Map(), shouldRetryOnError: false },
  }, children);

const mockAlert = (id = "a0"): Alert => ({
  id,
  symbol:     "SPY",
  condition:  "premium_gt",
  threshold:  100000,
  active:     true,
  created_at: "2026-04-29T00:00:00Z",
});

beforeEach(() => fetchMock.resetMocks());

describe("useAlerts", () => {
  it("fetches and returns alerts list", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert()] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.alerts).toHaveLength(1);
    expect(result.current.alerts[0].id).toBe("a0");
  });

  it("sets error when GET fetch rejects (network failure)", async () => {
    fetchMock.mockRejectOnce(new Error("401"));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  // Covers !res.ok branch in fetcher (lines 20-22 of useAlerts.ts)
  it("sets error and message when GET returns HTTP error status", async () => {
    fetchMock.mockResponseOnce("Unauthorized", { status: 401 });
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toContain("401");
  });

  it("create sends POST and revalidates", async () => {
    const newAlert = mockAlert("a2");
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));
    fetchMock.mockResponseOnce(JSON.stringify(newAlert));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [newAlert] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await act(async () => {
      await result.current.create({ symbol: "SPY", condition: "premium_gt", threshold: 100000, active: true });
    });
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    const postCall = fetchMock.mock.calls.find(
      (c: unknown[]) => (c[1] as RequestInit)?.method === "POST",
    );
    expect(postCall).toBeDefined();
  });

  // Covers !res.ok branch in mutationFetch (lines 47-49 of useAlerts.ts)
  it("create rolls back optimistic update when POST returns HTTP error", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));        // initial GET
    fetchMock.mockResponseOnce("Forbidden", { status: 403 });          // POST fails
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));        // SWR revalidation after rollback
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await act(async () => {
      try { await result.current.create({ symbol: "SPY", condition: "premium_gt", threshold: 100000, active: true }); } catch {}
    });
    await waitFor(() => expect(result.current.alerts).toHaveLength(0));
  });

  it("remove sends DELETE and revalidates", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert("a1")] }));
    fetchMock.mockResponseOnce(JSON.stringify({}));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    await act(async () => {
      await result.current.remove("a1");
    });
    await waitFor(() => expect(result.current.alerts).toHaveLength(0));
    const deleteCall = fetchMock.mock.calls.find(
      (c: unknown[]) => (c[1] as RequestInit)?.method === "DELETE",
    );
    expect(deleteCall).toBeDefined();
  });

  it("update sends PATCH and revalidates", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert("a1")] }));
    fetchMock.mockResponseOnce(JSON.stringify({ ...mockAlert("a1"), threshold: 200000 }));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [{ ...mockAlert("a1"), threshold: 200000 }] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    await act(async () => {
      await result.current.update("a1", { threshold: 200000 });
    });
    await waitFor(() => expect(result.current.alerts[0].threshold).toBe(200000));
    const patchCall = fetchMock.mock.calls.find(
      (c: unknown[]) => (c[1] as RequestInit)?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
  });

  // Covers the refresh: () => mutate() return value (line ~121 of useAlerts.ts)
  it("refresh triggers revalidation", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const before = fetchMock.mock.calls.length;
    act(() => { result.current.refresh(); });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
  });
});
