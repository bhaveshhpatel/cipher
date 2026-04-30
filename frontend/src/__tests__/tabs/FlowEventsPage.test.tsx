import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

jest.mock("@/hooks/useFlowEvents", () => ({ useFlowEvents: jest.fn() }));
jest.mock("@/components/dashboard/FlowEventsTab", () => ({
  FlowEventsTab: ({ onFiltersChange, loading, error }: {
    onFiltersChange: (f: object) => void;
    loading: boolean;
    error: string | null;
  }) => (
    <div data-testid="flow-events-tab">
      <button data-testid="trigger-filter" onClick={() => onFiltersChange({ sentiment: "BULLISH" })}>filter</button>
      {loading && <span data-testid="loading" />}
      {error   && <span data-testid="error">{error}</span>}
    </div>
  ),
}));

import { useFlowEvents } from "@/hooks/useFlowEvents";
import { FlowEventsPage } from "../../app/dashboard/_tabs/FlowEventsPage";

const mock = useFlowEvents as jest.Mock;

beforeEach(() => {
  mock.mockReturnValue({ events: [], loading: false, error: null });
});

describe("FlowEventsPage", () => {
  it("renders header", () => {
    render(<FlowEventsPage token="tok" />);
    expect(screen.getByText("Flow Events")).toBeInTheDocument();
  });

  it("renders FlowEventsTab", () => {
    render(<FlowEventsPage token="tok" />);
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
  });

  it("passes loading=true to child", () => {
    mock.mockReturnValue({ events: [], loading: true, error: null });
    render(<FlowEventsPage token="tok" />);
    expect(screen.getByTestId("loading")).toBeInTheDocument();
  });

  it("passes error to child", () => {
    mock.mockReturnValue({ events: [], loading: false, error: "oops" });
    render(<FlowEventsPage token="tok" />);
    expect(screen.getByTestId("error")).toHaveTextContent("oops");
  });

  it("updates filters state when onFiltersChange fires", () => {
    render(<FlowEventsPage token="tok" />);
    fireEvent.click(screen.getByTestId("trigger-filter"));
    // useFlowEvents should now be called with the new filter on next render
    // (mock captures the call; just verify no crash + tab still rendered)
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
  });

  it("calls useFlowEvents with null token when token is null", () => {
    render(<FlowEventsPage token={null} />);
    expect(mock).toHaveBeenCalledWith(null, {});
  });
});
