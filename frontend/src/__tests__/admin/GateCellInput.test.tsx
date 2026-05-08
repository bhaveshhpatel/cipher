/**
 * GateCellInput.test.tsx — 100% coverage for GateCellInput component
 * ADMIN-UI-001 | Chunk 3
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { GateCellInput } from "../../app/admin/_cards/GateCellInput";
import type { GateRow, SaveStatus } from "@/types/gates";

const BASE_ROW: GateRow = {
  gate_name:        "min_premium",
  tier:             1,
  value:            10000,
  min_value:        1000,
  max_value:        500000,
  tier_independent: false,
};

const MS_ROW: GateRow = {
  gate_name:        "dedup_window_ms",
  tier:             1,
  value:            5000,        // stored as ms; displayed as 5s
  min_value:        100,
  max_value:        60000,
  tier_independent: false,
};

const TOGGLE_ROW: GateRow = {
  gate_name:        "require_oi",
  tier:             1,
  value:            1,
  min_value:        0,
  max_value:        1,
  tier_independent: true,
};

function mkProps(overrides: Partial<{ row: GateRow; status: SaveStatus; onSave: jest.Mock }> = {}) {
  return {
    row:    overrides.row    ?? BASE_ROW,
    status: overrides.status ?? "idle",
    onSave: overrides.onSave ?? jest.fn(),
  };
}

describe("GateCellInput — number gate (min_premium)", () => {
  it("renders number input with correct initial value", () => {
    render(<GateCellInput {...mkProps()} />);
    const input = screen.getByRole("spinbutton");
    expect((input as HTMLInputElement).value).toBe("10000");
  });

  it("renders $ prefix for DOLLAR_GATES", () => {
    render(<GateCellInput {...mkProps()} />);
    expect(screen.getByText("$")).toBeInTheDocument();
  });

  it("shows Save button disabled when not dirty", () => {
    render(<GateCellInput {...mkProps()} />);
    expect(screen.getByText("Save")).toBeDisabled();
  });

  it("enables Save button when value changes", () => {
    render(<GateCellInput {...mkProps()} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "20000" } });
    expect(screen.getByText("Save")).not.toBeDisabled();
  });

  it("shows reason input when dirty and no validation error", () => {
    render(<GateCellInput {...mkProps()} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "20000" } });
    expect(screen.getByPlaceholderText("Reason (optional)")).toBeInTheDocument();
  });

  it("calls onSave with correct parsed value and reason on Save click", () => {
    const onSave = jest.fn();
    render(<GateCellInput {...mkProps({ onSave })} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "20000" } });
    fireEvent.change(screen.getByPlaceholderText("Reason (optional)"), {
      target: { value: "test reason" },
    });
    fireEvent.click(screen.getByText("Save"));
    expect(onSave).toHaveBeenCalledWith(20000, "test reason");
  });

  it("calls onSave with null reason when reason field is empty", () => {
    const onSave = jest.fn();
    render(<GateCellInput {...mkProps({ onSave })} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "20000" } });
    fireEvent.click(screen.getByText("Save"));
    expect(onSave).toHaveBeenCalledWith(20000, null);
  });

  it("calls onSave on Enter keypress", () => {
    const onSave = jest.fn();
    render(<GateCellInput {...mkProps({ onSave })} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "20000" } });
    fireEvent.keyDown(screen.getByRole("spinbutton"), { key: "Enter" });
    expect(onSave).toHaveBeenCalledWith(20000, null);
  });

  it("shows validation error for non-numeric input", () => {
    render(<GateCellInput {...mkProps()} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "abc" } });
    expect(screen.getByTestId("err-min_premium-1")).toHaveTextContent("Must be a number");
  });

  it("shows validation error when below min_value", () => {
    render(<GateCellInput {...mkProps()} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "500" } });
    expect(screen.getByTestId("err-min_premium-1")).toHaveTextContent("Min 1000");
  });

  it("shows validation error when above max_value", () => {
    render(<GateCellInput {...mkProps()} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "999999" } });
    expect(screen.getByTestId("err-min_premium-1")).toHaveTextContent("Max 500000");
  });

  it("does not call onSave when validation error exists", () => {
    const onSave = jest.fn();
    render(<GateCellInput {...mkProps({ onSave })} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "500" } });
    fireEvent.click(screen.getByText("Save"));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not call onSave on Enter when validation error exists", () => {
    const onSave = jest.fn();
    render(<GateCellInput {...mkProps({ onSave })} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "999999" } });
    fireEvent.keyDown(screen.getByRole("spinbutton"), { key: "Enter" });
    expect(onSave).not.toHaveBeenCalled();
  });

  it("renders ✓ badge when status is 'saved'", () => {
    render(<GateCellInput {...mkProps({ status: "saved" })} />);
    expect(screen.getByTestId("badge-saved")).toBeInTheDocument();
  });

  it("renders ✗ badge when status is 'error'", () => {
    render(<GateCellInput {...mkProps({ status: "error" })} />);
    expect(screen.getByTestId("badge-error")).toBeInTheDocument();
  });

  it("renders … badge when status is 'saving'", () => {
    render(<GateCellInput {...mkProps({ status: "saving" })} />);
    expect(screen.getByTestId("badge-saving")).toBeInTheDocument();
  });

  it("renders no badge when status is 'idle'", () => {
    render(<GateCellInput {...mkProps({ status: "idle" })} />);
    expect(screen.queryByTestId("badge-saved")).not.toBeInTheDocument();
    expect(screen.queryByTestId("badge-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("badge-saving")).not.toBeInTheDocument();
  });

  it("disables input while saving", () => {
    render(<GateCellInput {...mkProps({ status: "saving" })} />);
    expect(screen.getByRole("spinbutton")).toBeDisabled();
  });

  it("wraps in correct data-testid", () => {
    render(<GateCellInput {...mkProps()} />);
    expect(screen.getByTestId("cell-min_premium-1")).toBeInTheDocument();
  });
});

describe("GateCellInput — ms gate (dedup_window_ms)", () => {
  it("displays stored ms value divided by 1000 (5000ms → 5)", () => {
    render(<GateCellInput {...mkProps({ row: MS_ROW })} />);
    expect((screen.getByRole("spinbutton") as HTMLInputElement).value).toBe("5");
  });

  it("renders 's' suffix for ms gates", () => {
    render(<GateCellInput {...mkProps({ row: MS_ROW })} />);
    expect(screen.getByText("s")).toBeInTheDocument();
  });

  it("calls onSave with value × 1000 (10s → 10000ms)", () => {
    const onSave = jest.fn();
    render(<GateCellInput {...mkProps({ row: MS_ROW, onSave })} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "10" } });
    fireEvent.click(screen.getByText("Save"));
    expect(onSave).toHaveBeenCalledWith(10000, null);
  });
});

describe("GateCellInput — toggle gate (require_oi)", () => {
  it("renders a switch button not a number input", () => {
    render(<GateCellInput {...mkProps({ row: TOGGLE_ROW })} />);
    expect(screen.getByRole("switch")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  });

  it("aria-checked is true when value is 1", () => {
    render(<GateCellInput {...mkProps({ row: TOGGLE_ROW })} />);
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("aria-checked is false when value is 0", () => {
    const offRow: GateRow = { ...TOGGLE_ROW, value: 0 };
    render(<GateCellInput {...mkProps({ row: offRow })} />);
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
  });

  it("calls onSave(0, null) when toggling OFF", () => {
    const onSave = jest.fn();
    render(<GateCellInput {...mkProps({ row: TOGGLE_ROW, onSave })} />);
    fireEvent.click(screen.getByRole("switch"));
    expect(onSave).toHaveBeenCalledWith(0, null);
  });

  it("calls onSave(1, null) when toggling ON", () => {
    const onSave = jest.fn();
    const offRow: GateRow = { ...TOGGLE_ROW, value: 0 };
    render(<GateCellInput {...mkProps({ row: offRow, onSave })} />);
    fireEvent.click(screen.getByRole("switch"));
    expect(onSave).toHaveBeenCalledWith(1, null);
  });

  it("disables toggle while saving", () => {
    render(<GateCellInput {...mkProps({ row: TOGGLE_ROW, status: "saving" })} />);
    expect(screen.getByRole("switch")).toBeDisabled();
  });
});
