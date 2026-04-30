/**
 * TierThresholdsCard.test.tsx — 8 cases
 * Mocks global.fetch — no real network calls.
 */
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { TierThresholdsCard } from "../../app/admin/_cards/TierThresholdsCard";

const MOCK_RESPONSE = {
  row: {
    id:                 1,
    updated_at:         "2026-04-30T00:00:00Z",
    updated_by:         null,
    is_active:          true,
    t1_min_volume:      500,
    t1_min_last_price:  0.5,
    t1_min_oi:          1000,
    t1_atm_pct:         5,
    t1_max_dte:         60,
    t2_min_volume:      200,
    t2_min_last_price:  0.3,
    t2_min_oi:          500,
    t2_atm_pct:         10,
    t2_max_dte:         90,
    t3_min_volume:      50,
    t3_min_last_price:  0.1,
    t3_min_oi:          100,
    t3_atm_pct:         15,
    t3_max_dte:         120,
  },
  cache: { warm: true, age_seconds: 30, ttl_seconds: 300 },
};

beforeEach(() => jest.clearAllMocks());

describe("TierThresholdsCard — states", () => {
  it("renders title and subtitle", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    expect(screen.getByText("Tier Thresholds")).toBeInTheDocument();
    expect(screen.getByText(/Screening parameters/)).toBeInTheDocument();
  });

  it("shows Loading… initially", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("does not fetch when token is null", () => {
    global.fetch = jest.fn() as jest.Mock;
    render(<TierThresholdsCard token={null} />);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("shows error when fetch fails", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 403 }) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    await waitFor(() => expect(screen.getByText(/HTTP 403/)).toBeInTheDocument());
  });

  it("renders all three tier headings after fetch", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_RESPONSE }) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    await waitFor(() => expect(screen.getByText("Tier 1")).toBeInTheDocument());
    expect(screen.getByText("Tier 2")).toBeInTheDocument();
    expect(screen.getByText("Tier 3")).toBeInTheDocument();
  });

  it("Save button is disabled when field is not dirty", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_RESPONSE }) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    await waitFor(() => expect(screen.getByText("Tier 1")).toBeInTheDocument());
    const row = screen.getByTestId("field-t1_min_volume");
    expect(within(row).getByText("Save")).toBeDisabled();
  });

  it("Save button enables after editing a field", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_RESPONSE }) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    await waitFor(() => expect(screen.getByText("Tier 1")).toBeInTheDocument());
    const row = screen.getByTestId("field-t1_min_volume");
    fireEvent.change(within(row).getByRole("textbox"), { target: { value: "600" } });
    expect(within(row).getByText("Save")).not.toBeDisabled();
  });

  it("Save calls PATCH with correct payload", async () => {
    global.fetch = jest.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => MOCK_RESPONSE })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ row: { ...MOCK_RESPONSE.row, t1_min_volume: 600 } }) }) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    await waitFor(() => expect(screen.getByText("Tier 1")).toBeInTheDocument());
    const row = screen.getByTestId("field-t1_min_volume");
    fireEvent.change(within(row).getByRole("textbox"), { target: { value: "600" } });
    fireEvent.click(within(row).getByText("Save"));
    await waitFor(() => {
      const patchCall = (global.fetch as jest.Mock).mock.calls[1];
      expect(patchCall[1].method).toBe("PATCH");
      expect(JSON.parse(patchCall[1].body)).toEqual({ updates: { t1_min_volume: 600 } });
    });
  });

  it("shows validation error for non-numeric input", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_RESPONSE }) as jest.Mock;
    render(<TierThresholdsCard token="tok" />);
    await waitFor(() => expect(screen.getByText("Tier 1")).toBeInTheDocument());
    const row = screen.getByTestId("field-t1_min_volume");
    fireEvent.change(within(row).getByRole("textbox"), { target: { value: "abc" } });
    fireEvent.click(within(row).getByText("Save"));
    expect(within(row).getByText("Must be a number")).toBeInTheDocument();
  });
});
