/**
 * DemoEngineCard.test.tsx  — 12 cases
 * DemoEngineCard is purely presentational — no mocks needed.
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { DemoEngineCard } from "../../app/admin/_cards/DemoEngineCard";

const MOCK_STATUS = {
  demo: {
    ticks_emitted:     42,
    signals_generated: 7,
    last_ticker:       "AAPL",
    started_at:        "2026-04-30T06:00:00Z",
  },
};

describe("DemoEngineCard", () => {
  it("renders title and subtitle", () => {
    render(<DemoEngineCard status={null} isRunning={false} loading={false} error={null} toggle={jest.fn()} />);
    expect(screen.getByText("Demo Engine")).toBeInTheDocument();
    expect(screen.getByText(/Simulated flow/)).toBeInTheDocument();
  });

  it("shows RUNNING pill when isRunning=true", () => {
    render(<DemoEngineCard status={null} isRunning={true} loading={false} error={null} toggle={jest.fn()} />);
    expect(screen.getByText(/RUNNING/)).toBeInTheDocument();
  });

  it("shows STOPPED pill when isRunning=false", () => {
    render(<DemoEngineCard status={null} isRunning={false} loading={false} error={null} toggle={jest.fn()} />);
    expect(screen.getByText(/STOPPED/)).toBeInTheDocument();
  });

  it("shows LOADING pill when loading=true", () => {
    render(<DemoEngineCard status={null} isRunning={false} loading={true} error={null} toggle={jest.fn()} />);
    expect(screen.getByText("LOADING…")).toBeInTheDocument();
  });

  it("renders stats when status provided", () => {
    render(<DemoEngineCard status={MOCK_STATUS} isRunning={true} loading={false} error={null} toggle={jest.fn()} />);
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
  });

  it("shows '—' for null last_ticker and started_at", () => {
    const s = { demo: { ticks_emitted: 0, signals_generated: 0, last_ticker: null, started_at: null } };
    render(<DemoEngineCard status={s} isRunning={false} loading={false} error={null} toggle={jest.fn()} />);
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("renders error banner when error is set", () => {
    render(<DemoEngineCard status={null} isRunning={false} loading={false} error="Network error" toggle={jest.fn()} />);
    expect(screen.getByText("Network error")).toBeInTheDocument();
  });

  it("Start button is disabled when isRunning=true", () => {
    render(<DemoEngineCard status={null} isRunning={true} loading={false} error={null} toggle={jest.fn()} />);
    expect(screen.getByTestId("btn-start")).toBeDisabled();
  });

  it("Stop button is disabled when isRunning=false", () => {
    render(<DemoEngineCard status={null} isRunning={false} loading={false} error={null} toggle={jest.fn()} />);
    expect(screen.getByTestId("btn-stop")).toBeDisabled();
  });

  it("Start button calls toggle(true) on click", () => {
    const toggle = jest.fn();
    render(<DemoEngineCard status={null} isRunning={false} loading={false} error={null} toggle={toggle} />);
    fireEvent.click(screen.getByTestId("btn-start"));
    expect(toggle).toHaveBeenCalledWith(true);
  });

  it("Stop button calls toggle(false) on click", () => {
    const toggle = jest.fn();
    render(<DemoEngineCard status={null} isRunning={true} loading={false} error={null} toggle={toggle} />);
    fireEvent.click(screen.getByTestId("btn-stop"));
    expect(toggle).toHaveBeenCalledWith(false);
  });

  it("both buttons disabled when loading=true", () => {
    render(<DemoEngineCard status={null} isRunning={false} loading={true} error={null} toggle={jest.fn()} />);
    expect(screen.getByTestId("btn-start")).toBeDisabled();
    expect(screen.getByTestId("btn-stop")).toBeDisabled();
  });
});
