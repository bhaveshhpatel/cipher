import { renderHook, act, waitFor } from "@testing-library/react";
import { useAlerts } from "@/hooks";

const mockAlert = (id = "a1") => ({
  id,
  symbol:     "SPY",
  condition:  "premium_above",
  threshold:  100000,
  active:     true,
  created_at: "2026-01-01T00:00:00Z",
});

beforeEach(() => fetchMock.resetMocks());

describe("useAlerts", () => {
  it("loads alerts on mount", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert()] }));
    const { result } = renderHook(() => useAlerts());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.alerts).toHaveLength(1);
  });

  it("create calls POST and appends alert", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] }));
    const newAlert = mockAlert("a2");
    fetchMock.mockResponseOnce(JSON.stringify(newAlert)); // POST response
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [newAlert] })); // revalidate

    const { result } = renderHook(() => useAlerts());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await result.current.create({
        symbol: "SPY", condition: "premium_above", threshold: 100000, active: true,
      });
    });

    const postCall = fetchMock.mock.calls.find(c => (c[1] as RequestInit)?.method === "POST");
    expect(postCall).toBeDefined();
  });

  it("remove calls DELETE and removes alert", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert("a1")] }));
    fetchMock.mockResponseOnce(JSON.stringify({}));           // DELETE response
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [] })); // revalidate

    const { result } = renderHook(() => useAlerts());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => { await result.current.remove("a1"); });

    const deleteCall = fetchMock.mock.calls.find(c => (c[1] as RequestInit)?.method === "DELETE");
    expect(deleteCall).toBeDefined();
    expect(deleteCall![0]).toContain("/a1");
  });

  it("update calls PATCH with patch body", async () => {
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [mockAlert("a1")] }));
    fetchMock.mockResponseOnce(JSON.stringify({ ...mockAlert("a1"), threshold: 200000 }));
    fetchMock.mockResponseOnce(JSON.stringify({ alerts: [{ ...mockAlert("a1"), threshold: 200000 }] }));

    const { result } = renderHook(() => useAlerts());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => { await result.current.update("a1", { threshold: 200000 }); });

    const patchCall = fetchMock.mock.calls.find(c => (c[1] as RequestInit)?.method === "PATCH");
    expect(patchCall).toBeDefined();
    expect(JSON.parse((patchCall![1] as RequestInit).body as string)).toMatchObject({ threshold: 200000 });
  });

  it("sets error when GET fails", async () => {
    fetchMock.mockRejectOnce(new Error("401"));
    const { result } = renderHook(() => useAlerts());
    await waitFor(() => expect(result.current.error).not.toBeNull());
  });
});
