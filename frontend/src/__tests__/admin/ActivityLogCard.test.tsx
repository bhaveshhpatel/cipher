/**
 * ActivityLogCard.test.tsx  — 16 cases
 * Mocks useActivityLog so no real fetch occurs.
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

jest.mock("@/hooks/useActivityLog");

import { useActivityLog } from "@/hooks/useActivityLog";
import { ActivityLogCard } from "../../app/admin/_cards/ActivityLogCard";

const mockUseActivityLog = useActivityLog as jest.Mock;

const EMPTY_STATE = { items: [], total: 0, count: 0, loading: false, error: null, refresh: jest.fn() };

const FAKE_ITEMS = [
  {
    id:          "r1",
    created_at:  "2026-04-30T07:00:00Z",
    admin_email: "admin@cipher.io",
    action:      "tier_thresholds.update",
    detail:      { updates: { t1_min_volume: 500 } },
    ip_address:  "10.0.0.1",
  },
  {
    id:          "r2",
    created_at:  "2026-04-30T06:30:00Z",
    admin_email: "admin@cipher.io",
    action:      "demo.start",
    detail:      {},
    ip_address:  null,
  },
];

beforeEach(() => {
  jest.clearAllMocks();
  mockUseActivityLog.mockReturnValue(EMPTY_STATE);
});

describe("ActivityLogCard — states", () => {
  it("renders title and subtitle", () => {
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText("Activity Log")).toBeInTheDocument();
    expect(screen.getByText(/Audit trail/)).toBeInTheDocument();
  });

  it("shows Loading\u2026 while loading", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, loading: true });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows error message when error is set", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, error: "HTTP 500" });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText("HTTP 500")).toBeInTheDocument();
  });

  it("shows empty state message when no items", () => {
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText(/No log entries found/)).toBeInTheDocument();
  });

  it("renders table rows for each item", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: FAKE_ITEMS, total: 2, count: 2 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText("tier_thresholds.update")).toBeInTheDocument();
    expect(screen.getByText("demo.start")).toBeInTheDocument();
    expect(screen.getAllByText("admin@cipher.io").length).toBe(2);
  });

  it("renders '—' for empty detail and null ip_address", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: [FAKE_ITEMS[1]], total: 1, count: 1 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("renders detail JSON when detail is non-empty", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: [FAKE_ITEMS[0]], total: 1, count: 1 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText(/t1_min_volume/)).toBeInTheDocument();
  });
});

describe("ActivityLogCard — filters", () => {
  it("shows Clear button only when a filter is active", () => {
    render(<ActivityLogCard token="tok" />);
    expect(screen.queryByText(/Clear/)).toBeNull();
  });

  it("shows Clear button after email filter is set", () => {
    render(<ActivityLogCard token="tok" />);
    fireEvent.change(screen.getByPlaceholderText("Filter by email"), { target: { value: "a@b.com" } });
    expect(screen.getByText(/Clear/)).toBeInTheDocument();
  });

  it("Clear resets filters — hook called with nulls", () => {
    render(<ActivityLogCard token="tok" />);
    fireEvent.change(screen.getByPlaceholderText("Filter by email"), { target: { value: "a@b.com" } });
    fireEvent.click(screen.getByText(/Clear/));
    const lastCall = mockUseActivityLog.mock.calls[mockUseActivityLog.mock.calls.length - 1][0];
    expect(lastCall.adminEmail).toBeNull();
  });

  it("action filter change passes action to hook", () => {
    render(<ActivityLogCard token="tok" />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "demo.start" } });
    const lastCall = mockUseActivityLog.mock.calls[mockUseActivityLog.mock.calls.length - 1][0];
    expect(lastCall.action).toBe("demo.start");
  });

  it("shows total count label when total > 0", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: FAKE_ITEMS, total: 47, count: 2 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText("47 total")).toBeInTheDocument();
  });
});

describe("ActivityLogCard — pagination", () => {
  it("does not show pagination when total <= PAGE_SIZE (20)", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: FAKE_ITEMS, total: 2, count: 2 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.queryByText("← Prev")).toBeNull();
  });

  it("shows pagination when total > PAGE_SIZE", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: FAKE_ITEMS, total: 47, count: 20 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText("← Prev")).toBeInTheDocument();
    expect(screen.getByText("Next →")).toBeInTheDocument();
  });

  it("Prev button is disabled on first page", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: FAKE_ITEMS, total: 47, count: 20 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByLabelText("Previous page")).toBeDisabled();
  });

  it("shows correct page X of Y label", () => {
    mockUseActivityLog.mockReturnValue({ ...EMPTY_STATE, items: FAKE_ITEMS, total: 47, count: 20 });
    render(<ActivityLogCard token="tok" />);
    expect(screen.getByText(/Page 1 of 3/)).toBeInTheDocument();
  });
});
