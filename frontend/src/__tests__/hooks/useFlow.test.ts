/**
 * useFlow tests
 * Uses jest-fetch-mock to intercept fetch calls.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { useFlow } from "@/hooks";

const mockPage = (events: object[], overrides = {}) => ({
  events,
  total: events.length,
  page: 1,
  limit: 50,
  ...overrides,
});

beforeEach(() => {
  fetchMock.resetMocks();
});

describe("useFlow", () => {
  it("returns isLoading=true initially then events", async () => {
    const evt = { id: "1", symbol: "SPY", premium: 100000 };
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([evt])));

    const { result } = renderHook(() => useFlow());
    expect(result.current.isLoading).toBe(true);

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].id).toBe("1");
  });

  it("isEmpty=true when no events returned", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    const { result } = renderHook(() => useFlow());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isEmpty).toBe(true);
  });

  it("appends symbol param to URL when provided", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([])));
    renderHook(() => useFlow({ symbol: "aapl" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("symbol=AAPL");
  });

  it("returns error when fetch fails", async () => {
    fetchMock.mockRejectOnce(new Error("Network error"));
    const { result } = renderHook(() => useFlow());
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  it("hasMore=false when page has fewer events than pageSize", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockPage([{ id: "1" }])));
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
