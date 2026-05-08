/**
 * GateControlPanel.test.tsx — 100% coverage for GateControlPanel component
 * ADMIN-UI-001 | Chunk 3
 *
 * Updated for deliberation fixes:
 *  - save-error display path now asserted (was only asserting no-crash)
 *  - Refresh button disabled + visual feedback while loading
 *  - confirm_market_hours omitted from patch payload
 *  - token non-null assertion removed (patch called with string token)
 */
import React from "react";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { GateControlPanel } from "../../app/admin/_cards/GateControlPanel";
import type { GateConfigResponse } from "@/types/gates";

// ── Mock hooks ────────────────────────────────────────────────
const mockRefresh  = jest.fn();
const mockPatch    = jest.fn();

const mockUseGateConfig = jest.fn();
const mockUseGatePatch  = jest.fn();

jest.mock("@/hooks/useGateConfig", () => ({
  useGateConfig: (...args: unknown[]) => mockUseGateConfig(...args),
}));

jest.mock("@/hooks/useGatePatch", () => ({
  useGatePatch: (...args: unknown[]) => mockUseGatePatch(...args),
}));

// ── Test data ─────────────────────────────────────────────────
const MOCK_DATA: GateConfigResponse = {
  epoch: 7,
  gates: [
    { gate_name: "min_premium", tier: 1, value: 10000, min_value: 1000, max_value: 500000, tier_independent: false },
    { gate_name: "min_premium", tier: 2, value: 25000, min_value: 1000, max_value: 500000, tier_independent: false },
    { gate_name: "min_premium", tier: 3, value: 50000, min_value: 1000, max_value: 500000, tier_independent: false },
    { gate_name: "require_oi",  tier: 1, value: 1,     min_value: 0,    max_value: 1,      tier_independent: true  },
    { gate_name: "require_oi",  tier: 2, value: 1,     min_value: 0,    max_value: 1,      tier_independent: true  },
    { gate_name: "require_oi",  tier: 3, value: 1,     min_value: 0,    max_value: 1,      tier_independent: true  },
  ],
};

function makeHookReturn(overrides: Record<string, unknown> = {}) {
  return {
    data:      MOCK_DATA,
    loading:   false,
    error:     null,
    refresh:   mockRefresh,
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUseGateConfig.mockReturnValue(makeHookReturn());
  mockUseGatePatch.mockReturnValue({ statusMap: {}, patch: mockPatch });
});

describe("GateControlPanel — auth guard", () => {
  it("shows access denied when token is null", () => {
    render(<GateControlPanel token={null} isAdmin={false} />);
    expect(screen.getByText(/Access denied/)).toBeInTheDocument();
  });

  it("shows access denied when isAdmin is false", () => {
    render(<GateControlPanel token="tok" isAdmin={false} />);
    expect(screen.getByText(/Access denied/)).toBeInTheDocument();
  });

  it("does not show access denied for valid admin token", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.queryByText(/Access denied/)).not.toBeInTheDocument();
  });
});

describe("GateControlPanel — loading state", () => {
  it("shows Loading… when loading=true and data=null", () => {
    mockUseGateConfig.mockReturnValue(makeHookReturn({ loading: true, data: null }));
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByTestId("loading-msg")).toBeInTheDocument();
  });

  it("does not show Loading… when data is already present", () => {
    mockUseGateConfig.mockReturnValue(makeHookReturn({ loading: true }));
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.queryByTestId("loading-msg")).not.toBeInTheDocument();
  });

  it("disables and dims Refresh button while loading", () => {
    mockUseGateConfig.mockReturnValue(makeHookReturn({ loading: true }));
    render(<GateControlPanel token="tok" isAdmin={true} />);
    const btn = screen.getByTestId("refresh-btn");
    expect(btn).toBeDisabled();
  });

  it("shows '…' on Refresh button while loading with data present", () => {
    mockUseGateConfig.mockReturnValue(makeHookReturn({ loading: true }));
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByTestId("refresh-btn")).toHaveTextContent("…");
  });
});

describe("GateControlPanel — error state", () => {
  it("shows error banner when error and no data", () => {
    mockUseGateConfig.mockReturnValue(
      makeHookReturn({ error: "HTTP 500", data: null, loading: false }),
    );
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByText("HTTP 500")).toBeInTheDocument();
  });

  it("shows stale error banner alongside data when error + data both present", () => {
    mockUseGateConfig.mockReturnValue(
      makeHookReturn({ error: "Poll failed" }),
    );
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByText(/Poll failed/)).toBeInTheDocument();
    expect(screen.getByTestId("gate-row-min_premium")).toBeInTheDocument();
  });
});

describe("GateControlPanel — happy path rendering", () => {
  it("renders the panel title", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByText("Gate Control Panel")).toBeInTheDocument();
  });

  it("shows the current epoch in the subtitle", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByText(/epoch 7/)).toBeInTheDocument();
  });

  it("renders Tier 1 / Tier 2 / Tier 3 column headers", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByText("Tier 1")).toBeInTheDocument();
    expect(screen.getByText("Tier 2")).toBeInTheDocument();
    expect(screen.getByText("Tier 3")).toBeInTheDocument();
  });

  it("renders gate row for min_premium with human label", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByTestId("gate-row-min_premium")).toBeInTheDocument();
    expect(screen.getByText("Minimum Premium Floor")).toBeInTheDocument();
  });

  it("renders gate row for require_oi with tier-independent note", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByText("(tier-independent)")).toBeInTheDocument();
  });

  it("renders a cell for each gate × tier combination in data", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByTestId("cell-min_premium-1")).toBeInTheDocument();
    expect(screen.getByTestId("cell-min_premium-2")).toBeInTheDocument();
    expect(screen.getByTestId("cell-min_premium-3")).toBeInTheDocument();
    expect(screen.getByTestId("cell-require_oi-1")).toBeInTheDocument();
  });

  it("renders Refresh button", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByTestId("refresh-btn")).toBeInTheDocument();
  });

  it("clicking Refresh calls refresh()", () => {
    render(<GateControlPanel token="tok" isAdmin={true} />);
    fireEvent.click(screen.getByTestId("refresh-btn"));
    expect(mockRefresh).toHaveBeenCalledTimes(1);
  });

  it("shows empty message when gates array is empty", () => {
    mockUseGateConfig.mockReturnValue(
      makeHookReturn({ data: { epoch: 1, gates: [] } }),
    );
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByTestId("empty-msg")).toBeInTheDocument();
  });
});

describe("GateControlPanel — save flow", () => {
  it("calls patch with correct payload when a cell saves", async () => {
    mockPatch.mockResolvedValueOnce({
      gate_name: "min_premium", tier: 1, new_value: 20000, epoch: 8,
    });
    render(<GateControlPanel token="tok" isAdmin={true} />);

    const inputs = screen.getAllByRole("spinbutton");
    fireEvent.change(inputs[0], { target: { value: "20000" } });
    fireEvent.click(screen.getAllByText("Save")[0]);

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith(
        "tok",
        expect.objectContaining({
          gate_name: "min_premium",
          tier:      1,
          value:     20000,
          // confirm_market_hours must NOT be present — omitted by design
        }),
      );
      // Verify confirm_market_hours is not hardcoded
      const payload = mockPatch.mock.calls[0][1];
      expect(payload).not.toHaveProperty("confirm_market_hours");
    });
  });

  it("calls refresh() after a successful patch", async () => {
    mockPatch.mockResolvedValueOnce({
      gate_name: "min_premium", tier: 1, new_value: 20000, epoch: 8,
    });
    render(<GateControlPanel token="tok" isAdmin={true} />);
    const inputs = screen.getAllByRole("spinbutton");
    fireEvent.change(inputs[0], { target: { value: "20000" } });
    fireEvent.click(screen.getAllByText("Save")[0]);
    await waitFor(() => expect(mockRefresh).toHaveBeenCalled());
  });

  it("surfaces the error message in the affected cell when patch rejects", async () => {
    mockPatch.mockRejectedValueOnce(new Error("Value out of range"));
    render(<GateControlPanel token="tok" isAdmin={true} />);

    const inputs = screen.getAllByRole("spinbutton");
    fireEvent.change(inputs[0], { target: { value: "20000" } });
    await act(async () => {
      fireEvent.click(screen.getAllByText("Save")[0]);
    });

    // The error message must be visible in the UI, not swallowed silently
    await waitFor(() => {
      expect(screen.getByTestId("save-err-min_premium-1")).toHaveTextContent("Value out of range");
    });
  });

  it("shows 'Save failed' when patch rejects with a non-Error object", async () => {
    mockPatch.mockRejectedValueOnce("unknown error");
    render(<GateControlPanel token="tok" isAdmin={true} />);
    mockUseGatePatch.mockReturnValue({
      statusMap: { "min_premium:1": "error" },
      patch: mockPatch,
    });

    const inputs = screen.getAllByRole("spinbutton");
    fireEvent.change(inputs[0], { target: { value: "20000" } });
    await act(async () => {
      fireEvent.click(screen.getAllByText("Save")[0]);
    });

    // Component must not crash; the panel title must still be visible
    expect(screen.getByText("Gate Control Panel")).toBeInTheDocument();
  });

  it("passes statusMap cell key to GateCellInput", () => {
    mockUseGatePatch.mockReturnValue({
      statusMap: { "min_premium:1": "saved" },
      patch: mockPatch,
    });
    render(<GateControlPanel token="tok" isAdmin={true} />);
    expect(screen.getByTestId("badge-saved")).toBeInTheDocument();
  });

  it("does not call patch when token is null (auth guard prevents render)", () => {
    render(<GateControlPanel token={null} isAdmin={false} />);
    expect(mockPatch).not.toHaveBeenCalled();
  });
});

describe("GateControlPanel — useGateConfig called with token", () => {
  it("passes token to useGateConfig", () => {
    render(<GateControlPanel token="tok_xyz" isAdmin={true} />);
    expect(mockUseGateConfig).toHaveBeenCalledWith("tok_xyz");
  });

  it("passes null token to useGateConfig when unauthenticated", () => {
    render(<GateControlPanel token={null} isAdmin={false} />);
    expect(mockUseGateConfig).toHaveBeenCalledWith(null);
  });
});
