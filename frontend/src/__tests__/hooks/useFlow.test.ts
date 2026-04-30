/**
 * useFlow tests — SWR infinite pagination hook.
 *
 * Each test uses a fresh SWR cache (provider: () => new Map()) and
 * shouldRetryOnError: false so error propagation is synchronous.
 */
import React from 'react';
import { renderHook, waitFor, act } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useFlow } from "@/hooks";

const Wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(SWRConfig, {
    value: { provider: () => new Map(), shouldRetryOnError: false },
  }, children);

const mockPage = (events: object[], overrides = {}) => ({
  events,
  total: events.length,
  limit: 50,
  offset: 0,
  ...overrides,
});

const mockEvent = () => ({
  id: 1, ticker: "SPY", strike: 450, expiry: "2026-05-16",
  contract_type: "C", sentiment: "bullish", premium: 100000,
  size: 10, bid: 1.0, ask: 1.1, fill_price: 1.05,
  tier: "1", is_aggressive: true, is_golden_sweep: false,
  timestamp: "2026-04-29T20:00:00Z", session_date: "2026-04-29",
});

beforeEach(() => {
  fetchMock.resetMocks();
});

describe("useFlow", () => {
  it("returns isLoading=true initially, then resolves events", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([mockEvent()])));
    const { result } = renderHook(() => useFlow(), { wrapper: Wrapper });
    expect(result.current.isLoading).toBe(true);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].id).toBe(1);
  });

  it("isEmpty=true when server returns empty events array", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    const { result } = renderHook(() => useFlow(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isEmpty).toBe(true);
  });

  it("appends uppercase ticker param to URL when symbol provided", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    renderHook(() => useFlow({ symbol: "aapl" }), { wrapper: Wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("ticker=AAPL");
  });

  it("sets error when network rejects", async () => {
    fetchMock.mockRejectOnce(new Error("Network error"));
    const { result } = renderHook(() => useFlow(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  it("sets error and message when server returns non-200", async () => {
    fetchMock.mockResponseOnce("Unauthorized", { status: 401 });
    const { result } = renderHook(() => useFlow(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toContain("401");
  });

  it("hasMore=false when page has fewer events than pageSize", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([{ id: 1 }])));
    const { result } = renderHook(() => useFlow({ pageSize: 50 }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.hasMore).toBe(false);
  });

  it("paused=true does not fetch and returns empty events", async () => {
    const { result } = renderHook(() => useFlow({ paused: true }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.events).toHaveLength(0);
  });

  it("loadMore increments page and fetches next slice", async () => {
    const fullPage = Array.from({ length: 50 }, (_, i) => ({ id: i + 1 }));
    fetchMock.mockResponseOnce(JSON.stringify(mockPage(fullPage)));
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([{ id: 51 }])));
    const { result } = renderHook(() => useFlow({ pageSize: 50 }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.page).toBe(1);
    act(() => { result.current.loadMore(); });
    await waitFor(() => expect(result.current.page).toBe(2));
    await waitFor(() => expect(result.current.events).toHaveLength(51));
  });

  it("refresh triggers a new fetch", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    const { result } = renderHook(() => useFlow(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callsBefore = fetchMock.mock.calls.length;
    act(() => { result.current.refresh(); });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore));
  });
});
