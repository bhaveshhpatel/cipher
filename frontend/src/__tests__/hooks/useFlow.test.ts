/**
 * useFlow tests
 * Uses jest-fetch-mock to intercept fetch calls.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { useFlow } from "@/hooks";

const mockPage = (events: object[], overrides = {}) => ({
  events,
  total: events.length,
  limit: 50,
  offset: 0,
  ...overrides,
});

beforeEach(() => {
  fetchMock.resetMocks();
});

describe("useFlow", () => {
  it("returns isLoading=true initially then events", async () => {
    const evt = { id: 1, ticker: "SPY", premium: 100000, strike: 450, expiry: "2026-05-16",
                  contract_type: "C", sentiment: "bullish", size: 10, bid: 1.0, ask: 1.1,
                  fill_price: 1.05, tier: "1", is_aggressive: true, is_golden_sweep: false,
                  timestamp: "2026-04-29T20:00:00Z", session_date: "2026-04-29" };
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([evt])));

    const { result } = renderHook(() => useFlow());
    expect(result.current.isLoading).toBe(true);

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].id).toBe(1);
  });

  it("isEmpty=true when no events returned", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    const { result } = renderHook(() => useFlow());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isEmpty).toBe(true);
  });

  it("appends ticker param to URL when symbol provided", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    renderHook(() => useFlow({ symbol: "aapl" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("ticker=AAPL");
  });

  it("returns error when fetch fails", async () => {
    fetchMock.mockRejectOnce(new Error("Network error"));
    const { result } = renderHook(() => useFlow());
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  it("hasMore=false when page has fewer events than pageSize", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([{ id: 1 }])));
    const { result } = renderHook(() => useFlow({ pageSize: 50 }));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.hasMore).toBe(false);
  });

  it("paused=true returns no events and does not fetch", async () => {
    const { result } = renderHook(() => useFlow({ paused: true }));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.events).toHaveLength(0);
  });
});
