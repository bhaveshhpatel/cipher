import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

jest.mock("@/hooks/useSimulation", () => ({ useSimulation: jest.fn() }));
jest.mock("@/lib/api", () => ({
  api: {
    getFlow: jest.fn().mockResolvedValue({ events: [] }),
  },
}));
jest.mock("@/components/dashboard/SimulationPanel", () => ({
  SimulationPanel: () => <div data-testid="simulation-panel" />,
}));

import { useSimulation } from "@/hooks/useSimulation";
import { api } from "@/lib/api";
import { SimulationPage } from "../../app/dashboard/_tabs/SimulationPage";

const mockSim = useSimulation as jest.Mock;
const mockApi = api.getFlow as jest.Mock;

const DEFAULT_SIM = { result: null, loading: false, error: null, progress: 0, run: jest.fn() };

beforeEach(() => {
  mockSim.mockReturnValue({ ...DEFAULT_SIM });
  mockApi.mockResolvedValue({ events: [] });
  jest.clearAllMocks();
});

describe("SimulationPage", () => {
  it("renders header", () => {
    render(<SimulationPage token="tok" />);
    expect(screen.getByText("AI Swarm Simulation")).toBeInTheDocument();
  });

  it("renders SimulationPanel", () => {
    render(<SimulationPage token="tok" />);
    expect(screen.getByTestId("simulation-panel")).toBeInTheDocument();
  });

  it("renders countdown", async () => {
    render(<SimulationPage token="tok" />);
    await waitFor(() => expect(screen.getByTestId("flow-countdown")).toBeInTheDocument());
  });

  it("does NOT show Re-run button when events are empty", async () => {
    render(<SimulationPage token="tok" />);
    await waitFor(() => expect(mockApi).toHaveBeenCalled());
    expect(screen.queryByTestId("rerun-btn")).not.toBeInTheDocument();
  });

  it("shows Re-run button when events are returned", async () => {
    mockApi.mockResolvedValue({ events: [{ ticker: "AAPL", id: "1" }] });
    render(<SimulationPage token="tok" />);
    await waitFor(() => expect(screen.getByTestId("rerun-btn")).toBeInTheDocument());
  });

  it("calls getFlow on mount with empty ticker", async () => {
    render(<SimulationPage token="tok" />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("", "tok"));
  });

  it("does not call getFlow when token is null", () => {
    render(<SimulationPage token={null} />);
    expect(mockApi).not.toHaveBeenCalled();
  });

  it("calls run when Re-run button clicked", async () => {
    const run = jest.fn();
    mockSim.mockReturnValue({ ...DEFAULT_SIM, run });
    mockApi.mockResolvedValue({ events: [{ ticker: "SPY", id: "e1" }] });
    render(<SimulationPage token="tok" />);
    await waitFor(() => screen.getByTestId("rerun-btn"));
    fireEvent.click(screen.getByTestId("rerun-btn"));
    expect(run).toHaveBeenCalledWith("SPY", expect.any(Array), 6, 3);
  });
});
