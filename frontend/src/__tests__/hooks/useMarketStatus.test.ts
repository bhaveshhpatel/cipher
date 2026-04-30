import { renderHook, waitFor } from "@testing-library/react";
import { useMarketStatus } from "@/hooks";

const openResponse  = { status: "open",   next_change: "2026-04-30T16:00:00Z", session: "regular" };
const closedResponse = { status: "closed", next_change: "2026-04-30T09:30:00Z", session: "closed" };

beforeEach(() => fetchMock.resetMocks());

describe("useMarketStatus", () => {
  it("returns status=open and isOpen=true", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    const { result } = renderHook(() => useMarketStatus());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.status).toBe("open");
    expect(result.current.isOpen).toBe(true);
  });

  it("returns isOpen=false for closed status", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(closedResponse));
    const { result } = renderHook(() => useMarketStatus());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isOpen).toBe(false);
  });

  it("exposes nextChange and session", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    const { result } = renderHook(() => useMarketStatus());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.nextChange).toBe("2026-04-30T16:00:00Z");
    expect(result.current.session).toBe("regular");
  });

  it("sets error on fetch failure", async () => {
    fetchMock.mockRejectOnce(new Error("503"));
    const { result } = renderHook(() => useMarketStatus());
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  it("status=null before first fetch resolves", () => {
    fetchMock.mockResponseOnce(JSON.stringify(openResponse));
    const { result } = renderHook(() => useMarketStatus());
    expect(result.current.status).toBeNull();
  });
});
