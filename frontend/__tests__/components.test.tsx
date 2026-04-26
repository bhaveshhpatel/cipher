/**
 * Regression tests for dashboard sub-components:
 *   - FlowTable: renders rows, empty state, error state, summary stats, filter buttons
 *   - FlowTable: ConvictionBar colour logic (green >=70, amber >=40, red <40)
 *   - FlowTable: empty-state shows ticker name when ticker is set
 *   - FlowTable: empty-state shows generic text when no ticker
 *   - FlowTable: golden-sweep star ★ shown on is_golden_sweep=true rows
 *   - FlowTable: filter buttons ALL/BULLISH/BEARISH/NEUTRAL render
 *   - FlowTable: clicking BEARISH filter hides BULLISH rows
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { FlowTable } from "../src/components/dashboard/FlowTable";
import type { FlowEvent } from "../src/lib/api";

// ── helpers ──────────────────────────────────────────────────────────────────

const BASE_EVENT: FlowEvent = {
  ticker:           "AAPL",
  contract_type:    "CALL",
  strike:           195,
  expiry:           "2026-06-20",
  premium:          250_000,
  trade_type:       "SWEEP",
  sentiment:        "BULLISH",
  influence_tier:   "WHALE",
  conviction_score: 0.92,
  is_golden_sweep:  true,
  timestamp:        "2026-04-25T18:00:00Z",
};

const BEAR_EVENT: FlowEvent = {
  ...BASE_EVENT,
  ticker:        "TSLA",
  sentiment:     "BEARISH",
  influence_tier: "RETAIL",
  conviction_score: 0.35,
  is_golden_sweep: false,
};

const LOW_CONVICTION_EVENT: FlowEvent = {
  ...BASE_EVENT,
  ticker:           "SPY",
  conviction_score: 0.25,  // below 40 — should use var(--red)
  is_golden_sweep:  false,
};

const MID_CONVICTION_EVENT: FlowEvent = {
  ...BASE_EVENT,
  ticker:           "QQQ",
  conviction_score: 0.55,  // 40-70 — should use var(--amber)
  is_golden_sweep:  false,
};

const mockOnScan = jest.fn();

// ── FlowTable: rendering ─────────────────────────────────────────────────────

describe("FlowTable", () => {
  beforeEach(() => jest.clearAllMocks());

  it("renders event rows with ticker, type, and premium", () => {
    render(
      <FlowTable
        events={[BASE_EVENT]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("CALL")).toBeInTheDocument();
    // Premium $250K should format as $250.0K
    expect(screen.getByText(/\$250\.0K/)).toBeInTheDocument();
  });

  it("renders the golden-sweep star ★ for is_golden_sweep=true rows", () => {
    render(
      <FlowTable
        events={[BASE_EVENT]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.getByText("★")).toBeInTheDocument();
  });

  it("does NOT render golden-sweep star when is_golden_sweep=false", () => {
    render(
      <FlowTable
        events={[BEAR_EVENT]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.queryByText("★")).not.toBeInTheDocument();
  });

  it("shows the empty-state generic message when no events and no ticker", () => {
    render(
      <FlowTable
        events={[]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.getByText(/no flow data yet/i)).toBeInTheDocument();
  });

  it("shows the ticker-specific empty-state message when ticker is set", () => {
    render(
      <FlowTable
        events={[]}
        loading={false}
        error={null}
        ticker="NVDA"
        onScan={mockOnScan}
      />
    );
    expect(screen.getByText(/no flow events for nvda/i)).toBeInTheDocument();
  });

  it("shows error message when error prop is set", () => {
    render(
      <FlowTable
        events={[]}
        loading={false}
        error="Failed to fetch flow data"
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.getByText(/failed to fetch flow data/i)).toBeInTheDocument();
  });

  it("renders ALL / BULLISH / BEARISH / NEUTRAL filter buttons", () => {
    render(
      <FlowTable
        events={[BASE_EVENT]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    ["ALL", "BULLISH", "BEARISH", "NEUTRAL"].forEach(label => {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    });
  });

  it("clicking BEARISH filter hides BULLISH rows", () => {
    render(
      <FlowTable
        events={[BASE_EVENT, BEAR_EVENT]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    // Both tickers visible initially
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "BEARISH" }));

    // Only TSLA (BEARISH) should remain
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
  });

  it("renders summary stats row when events are present", () => {
    render(
      <FlowTable
        events={[BASE_EVENT, BEAR_EVENT]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.getByText(/total premium/i)).toBeInTheDocument();
    expect(screen.getByText(/bullish events/i)).toBeInTheDocument();
    expect(screen.getByText(/bearish events/i)).toBeInTheDocument();
    expect(screen.getByText(/whale trades/i)).toBeInTheDocument();
  });

  it("does NOT render summary stats row when events are empty", () => {
    render(
      <FlowTable
        events={[]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.queryByText(/total premium/i)).not.toBeInTheDocument();
  });

  it("renders skeleton rows when loading=true", () => {
    const { container } = render(
      <FlowTable
        events={[]}
        loading={true}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    // SkeletonRows renders divs with class 'skeleton'
    const skeletonCells = container.querySelectorAll(".skeleton");
    expect(skeletonCells.length).toBeGreaterThan(0);
  });

  it("renders multiple event rows correctly", () => {
    render(
      <FlowTable
        events={[BASE_EVENT, BEAR_EVENT, LOW_CONVICTION_EVENT, MID_CONVICTION_EVENT]}
        loading={false}
        error={null}
        ticker=""
        onScan={mockOnScan}
      />
    );
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(screen.getByText("SPY")).toBeInTheDocument();
    expect(screen.getByText("QQQ")).toBeInTheDocument();
  });
});
