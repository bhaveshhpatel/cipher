/**
 * IngestionConfigCard.test.tsx — 9 cases
 * Mocks global.fetch — no real network calls.
 */
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { IngestionConfigCard } from "../../app/admin/_cards/IngestionConfigCard";

const MOCK_CONFIG = [
  {
    key:         "batch_size",
    value:       "100",
    value_type:  "int",
    description: "Number of symbols per batch",
    updated_at:  "2026-04-30T00:00:00Z",
    updated_by:  null,
  },
  {
    key:         "poll_interval_ms",
    value:       "5000",
    value_type:  "int",
    description: "Polling interval in milliseconds",
    updated_at:  "2026-04-30T00:00:00Z",
    updated_by:  "admin@cipher.io",
  },
];

beforeEach(() => jest.clearAllMocks());

describe("IngestionConfigCard — states", () => {
  it("renders title and subtitle", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    expect(screen.getByText("Ingestion Config")).toBeInTheDocument();
    expect(screen.getByText(/Runtime config/)).toBeInTheDocument();
  });

  it("shows Loading… initially", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("does not fetch when token is null", () => {
    global.fetch = jest.fn() as jest.Mock;
    render(<IngestionConfigCard token={null} />);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("shows error when fetch fails", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 401 }) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    await waitFor(() => expect(screen.getByText(/HTTP 401/)).toBeInTheDocument());
  });

  it("shows empty state when no rows returned", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => [] }) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    await waitFor(() => expect(screen.getByText(/No config rows/)).toBeInTheDocument());
  });

  it("renders config keys after fetch", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_CONFIG }) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    await waitFor(() => expect(screen.getByText("batch_size")).toBeInTheDocument());
    expect(screen.getByText("poll_interval_ms")).toBeInTheDocument();
  });

  it("renders row descriptions", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_CONFIG }) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    await waitFor(() => expect(screen.getByText("Number of symbols per batch")).toBeInTheDocument());
  });

  it("Save button is disabled when field is not dirty", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_CONFIG }) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    await waitFor(() => expect(screen.getByText("batch_size")).toBeInTheDocument());
    const row = screen.getByTestId("row-batch_size");
    expect(within(row).getByText("Save")).toBeDisabled();
  });

  it("Save button enables after editing a field", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_CONFIG }) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    await waitFor(() => expect(screen.getByText("batch_size")).toBeInTheDocument());
    const row = screen.getByTestId("row-batch_size");
    fireEvent.change(within(row).getByRole("textbox"), { target: { value: "200" } });
    expect(within(row).getByText("Save")).not.toBeDisabled();
  });

  it("Save calls PATCH /api/admin/ingestion/config with correct body", async () => {
    global.fetch = jest.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => MOCK_CONFIG })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValueOnce({ ok: true, json: async () => MOCK_CONFIG }) as jest.Mock;
    render(<IngestionConfigCard token="tok" />);
    await waitFor(() => expect(screen.getByText("batch_size")).toBeInTheDocument());
    const row = screen.getByTestId("row-batch_size");
    fireEvent.change(within(row).getByRole("textbox"), { target: { value: "200" } });
    fireEvent.click(within(row).getByText("Save"));
    await waitFor(() => {
      const patchCall = (global.fetch as jest.Mock).mock.calls[1];
      expect(patchCall[1].method).toBe("PATCH");
      expect(JSON.parse(patchCall[1].body)).toEqual({ key: "batch_size", value: "200" });
    });
  });
});
