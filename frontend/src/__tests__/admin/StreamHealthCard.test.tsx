/**
 * StreamHealthCard.test.tsx — 7 cases
 * Mocks global.fetch — no real network calls.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { StreamHealthCard } from "../../app/admin/_cards/StreamHealthCard";

const MOCK_HEALTH = {
  mode:              "live",
  active_symbols:    150,
  ticks:             42000,
  classified:        38000,
  deduped:           37500,
  signals:           120,
  errors:            2,
  reconnects:        1,
  last_tick_at:      "2026-04-30T09:00:00Z",
  last_reconnect_at: null,
  uptime_seconds:    7200,
};

beforeEach(() => jest.clearAllMocks());

describe("StreamHealthCard — states", () => {
  it("renders title and subtitle", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<StreamHealthCard token="tok" />);
    expect(screen.getByText("Stream Health")).toBeInTheDocument();
    expect(screen.getByText(/auto-refreshes/)).toBeInTheDocument();
  });

  it("shows Loading… initially", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<StreamHealthCard token="tok" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("does not fetch when token is null", () => {
    global.fetch = jest.fn() as jest.Mock;
    render(<StreamHealthCard token={null} />);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("shows error when fetch fails", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 500 }) as jest.Mock;
    render(<StreamHealthCard token="tok" />);
    await waitFor(() => expect(screen.getByText(/HTTP 500/)).toBeInTheDocument());
  });

  it("renders counters after successful fetch", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_HEALTH }) as jest.Mock;
    render(<StreamHealthCard token="tok" />);
    await waitFor(() => expect(screen.getByText("150")).toBeInTheDocument());
    expect(screen.getByText("120")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("renders mode pill after fetch", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_HEALTH }) as jest.Mock;
    render(<StreamHealthCard token="tok" />);
    await waitFor(() => expect(screen.getByText(/● live/i)).toBeInTheDocument());
  });

  it("refresh button triggers an additional fetch", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_HEALTH }) as jest.Mock;
    render(<StreamHealthCard token="tok" />);
    await waitFor(() => expect(screen.getByText("150")).toBeInTheDocument());
    fireEvent.click(screen.getByText("↻"));
    expect((global.fetch as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
