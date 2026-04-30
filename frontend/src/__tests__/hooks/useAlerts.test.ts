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

  it("createAlert sends POST and revalidates", async () => {
    const newAlert = mockAlert("a2");
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));
    fetchMock.mockResponseOnce(JSON.stringify(newAlert));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [newAlert] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await act(async () => {
      await result.current.createAlert({ symbol: "SPY", condition: "premium_gt", threshold: 100000 });
    });
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    const postCall = fetchMock.mock.calls.find(
      (c: unknown[]) => (c[1] as RequestInit)?.method === "POST",
    );
    expect(postCall).toBeDefined();
  });

  it("deleteAlert sends DELETE and revalidates", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert("a1")] }));
    fetchMock.mockResponseOnce(JSON.stringify({}));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    await act(async () => {
      await result.current.deleteAlert("a1");
    });
    await waitFor(() => expect(result.current.alerts).toHaveLength(0));
    const deleteCall = fetchMock.mock.calls.find(
      (c: unknown[]) => (c[1] as RequestInit)?.method === "DELETE",
    );
    expect(deleteCall).toBeDefined();
  });

  it("updateAlert sends PATCH and revalidates", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert("a1")] }));
    fetchMock.mockResponseOnce(JSON.stringify({ ...mockAlert("a1"), threshold: 200000 }));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [{ ...mockAlert("a1"), threshold: 200000 }] }));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    await act(async () => {
      await result.current.updateAlert("a1", { threshold: 200000 });
    });
    await waitFor(() => expect(result.current.alerts[0].threshold).toBe(200000));
    const patchCall = fetchMock.mock.calls.find(
      (c: unknown[]) => (c[1] as RequestInit)?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
  });

  it("sets error when GET fails", async () => {
    fetchMock.mockRejectOnce(new Error("401"));
    const { result } = renderHook(() => useAlerts(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });
});
