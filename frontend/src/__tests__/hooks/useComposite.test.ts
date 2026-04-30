import React from 'react';
import { renderHook, waitFor, act } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useComposite } from "@/hooks";

const Wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(SWRConfig, {
    value: { provider: () => new Map(), shouldRetryOnError: false },
  }, children);

const mockSignal = {
  ticker:          "SPY",
  recommendation:  "BUY",
  composite_score: 0.87,
  flow_score:      0.80,
  backtest_score:  0.90,
  reasoning:       "Strong bullish flow",
};

beforeEach(() => fetchMock.resetMocks());

describe("useComposite", () => {
  it("returns signal after fetch", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    const { result } = renderHook(() => useComposite({ symbol: "SPY" }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.signal?.recommendation).toBe("BUY");
  });

  it("signal=null on initial render before fetch resolves", () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    const { result } = renderHook(() => useComposite(), { wrapper: Wrapper });
    expect(result.current.signal).toBeNull();
  });

  it("sets error when fetch rejects (network failure)", async () => {
    fetchMock.mockRejectOnce(new Error("500"));
    const { result } = renderHook(() => useComposite(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  // Covers the !res.ok branch inside fetcher (lines 22-24 of useComposite.ts)
  it("sets error and message when fetch returns HTTP error status", async () => {
    fetchMock.mockResponseOnce("Unauthorized", { status: 401 });
    const { result } = renderHook(() => useComposite(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toContain("401");
  });

  it("paused=true does not fetch", async () => {
    const { result } = renderHook(() => useComposite({ paused: true }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("appends uppercased symbol to URL", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    renderHook(() => useComposite({ symbol: "qqq" }), { wrapper: Wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("QQQ");
  });

  // Covers the refresh: () => mutate() return value (line 67 of useComposite.ts)
  it("refresh triggers revalidation", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    const { result } = renderHook(() => useComposite({ symbol: "SPY" }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const before = fetchMock.mock.calls.length;
    act(() => { result.current.refresh(); });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
  });
});
