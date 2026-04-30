import React from "react";
import { render, screen } from "@testing-library/react";

jest.mock("@/hooks/useFlowEpisodes", () => ({ useFlowEpisodes: jest.fn() }));
jest.mock("@/components/dashboard/FlowEpisodesTab", () => ({
  FlowEpisodesTab: ({ loading, error }: { loading: boolean; error: string | null }) => (
    <div data-testid="flow-episodes-tab">
      {loading && <span data-testid="loading" />}
      {error   && <span data-testid="error">{error}</span>}
    </div>
  ),
}));

import { useFlowEpisodes } from "@/hooks/useFlowEpisodes";
import { FlowEpisodesPage } from "../../app/dashboard/_tabs/FlowEpisodesPage";

const mock = useFlowEpisodes as jest.Mock;

beforeEach(() => {
  mock.mockReturnValue({ episodes: [], loading: false, error: null });
});

describe("FlowEpisodesPage", () => {
  it("renders header", () => {
    render(<FlowEpisodesPage token="tok" />);
    expect(screen.getByText("Repetition Episodes")).toBeInTheDocument();
  });

  it("renders FlowEpisodesTab", () => {
    render(<FlowEpisodesPage token="tok" />);
    expect(screen.getByTestId("flow-episodes-tab")).toBeInTheDocument();
  });

  it("passes loading to child", () => {
    mock.mockReturnValue({ episodes: [], loading: true, error: null });
    render(<FlowEpisodesPage token="tok" />);
    expect(screen.getByTestId("loading")).toBeInTheDocument();
  });

  it("passes error to child", () => {
    mock.mockReturnValue({ episodes: [], loading: false, error: "fail" });
    render(<FlowEpisodesPage token="tok" />);
    expect(screen.getByTestId("error")).toHaveTextContent("fail");
  });

  it("calls useFlowEpisodes with null token", () => {
    render(<FlowEpisodesPage token={null} />);
    expect(mock).toHaveBeenCalledWith(null, {});
  });
});
