import React from 'react';
import { renderHook, waitFor, act } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useMarketStatus } from "@/hooks";

const Wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(SWRConfig, {
    value: { provider: () => new Map(), shouldRetryOnError: false },
  }, children);

const openResponse   = { status: "open",   next_change: "2026-04-29T21:00:00Z", session: "regular" };
const closedResponse = { status: "closed", next_change: "2026-04-30T13:30:00Z", session: "none" };

beforeEach(() => fetchMock.resetMocks());

describe("useMarketStatus", () => {
  it("returns isOpen=true for open status", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    const { result } = renderHook(() => useMarketStatus(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isOpen).toBe(true);
    expect(result.current.status).toBe("open");
  });

  it("returns isOpen=false for closed status", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(closedResponse));
    const { result } = renderHook(() => useMarketStatus(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isOpen).toBe(false);
    expect(result.current.status).toBe("closed");
  });

  it("exposes nextChange and session from response", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    const { result } = renderHook(() => useMarketStatus(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.nextChange).toBe(openResponse.next_change);
    expect(result.current.session).toBe(openResponse.session);
  });

  it("sets error on fetch rejection (network failure)", async () => {
    fetchMock.mockRejectOnce(new Error("503"));
    const { result } = renderHook(() => useMarketStatus(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  // Covers the !res.ok branch inside fetcher (lines 22-24 of useMarketStatus.ts)
  it("sets error and message when fetch returns HTTP error status", async () => {
    fetchMock.mockResponseOnce("Service Unavailable", { status: 503 });
    const { result } = renderHook(() => useMarketStatus(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toContain("503");
  });

  it("starts with isLoading=true before first fetch resolves", () => {
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    const { result } = renderHook(() => useMarketStatus(), { wrapper: Wrapper });
    expect(result.current.isLoading).toBe(true);
  });

  // Covers the refresh: () => mutate() return value (line 61 of useMarketStatus.ts)
  it("refresh triggers revalidation", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    const { result } = renderHook(() => useMarketStatus(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const before = fetchMock.mock.calls.length;
    act(() => { result.current.refresh(); });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
  });
});
