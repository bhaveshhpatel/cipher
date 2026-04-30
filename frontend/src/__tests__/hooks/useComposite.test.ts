import { renderHook, waitFor } from "@testing-library/react";
import { useComposite } from "@/hooks";

const mockSignal = { symbol: "SPY", verdict: "BUY", tier: 1, score: 0.87 };

beforeEach(() => fetchMock.resetMocks());

describe("useComposite", () => {
  it("returns signal after fetch", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    const { result } = renderHook(() => useComposite({ symbol: "SPY" }));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.signal?.verdict).toBe("BUY");
  });

  it("signal=null on initial load", () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    const { result } = renderHook(() => useComposite());
    expect(result.current.signal).toBeNull();
  });

  it("sets error when fetch fails", async () => {
    fetchMock.mockRejectOnce(new Error("500"));
    const { result } = renderHook(() => useComposite());
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });

  it("paused=true does not fetch", async () => {
    const { result } = renderHook(() => useComposite({ paused: true }));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("appends symbol to URL", async () => {
    fetchMock.mockResponseOnce(JSON.stringify(mockSignal));
    renderHook(() => useComposite({ symbol: "qqq" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("symbol=QQQ");
  });
});
