import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => ({
  api: { getComposite: jest.fn() },
}));
jest.mock("@/components/dashboard/CompositeCard", () => ({
  CompositeCard: ({ ticker }: { ticker: string }) => (
    <div data-testid="composite-card" data-ticker={ticker} />
  ),
}));

import { api } from "@/lib/api";
import { CompositePage } from "../../app/dashboard/_tabs/CompositePage";

const mockGetComposite = api.getComposite as jest.Mock;

beforeEach(() => {
  mockGetComposite.mockResolvedValue(null);
  jest.clearAllMocks();
});

describe("CompositePage", () => {
  it("renders header", () => {
    render(<CompositePage token="tok" />);
    expect(screen.getByText("Composite Signal")).toBeInTheDocument();
  });

  it("shows empty state on mount", () => {
    render(<CompositePage token="tok" />);
    expect(screen.getByText(/Enter a ticker above/i)).toBeInTheDocument();
  });

  it("renders TickerSearchBar", () => {
    render(<CompositePage token="tok" />);
    expect(screen.getByTestId("ticker-search-bar")).toBeInTheDocument();
  });

  it("calls getComposite on form submit", async () => {
    render(<CompositePage token="tok" />);
    fireEvent.change(screen.getByTestId("ticker-input"), { target: { value: "AAPL" } });
    fireEvent.click(screen.getByTestId("ticker-submit"));
    await waitFor(() => expect(mockGetComposite).toHaveBeenCalledWith("AAPL", "tok"));
  });

  it("hides empty state after ticker is set", async () => {
    render(<CompositePage token="tok" />);
    fireEvent.change(screen.getByTestId("ticker-input"), { target: { value: "TSLA" } });
    fireEvent.click(screen.getByTestId("ticker-submit"));
    await waitFor(() => expect(screen.queryByText(/Enter a ticker above/i)).not.toBeInTheDocument());
  });

  it("renders CompositeCard", () => {
    render(<CompositePage token="tok" />);
    expect(screen.getByTestId("composite-card")).toBeInTheDocument();
  });

  it("shows error banner when getComposite rejects", async () => {
    mockGetComposite.mockRejectedValue(new Error("fetch failed"));
    render(<CompositePage token="tok" />);
    fireEvent.change(screen.getByTestId("ticker-input"), { target: { value: "SPY" } });
    fireEvent.click(screen.getByTestId("ticker-submit"));
    await waitFor(() => expect(screen.getByTestId("composite-error")).toHaveTextContent("fetch failed"));
  });

  it("clears error on handleClear", async () => {
    mockGetComposite.mockRejectedValue(new Error("oops"));
    render(<CompositePage token="tok" />);
    fireEvent.change(screen.getByTestId("ticker-input"), { target: { value: "SPY" } });
    fireEvent.click(screen.getByTestId("ticker-submit"));
    await waitFor(() => screen.getByTestId("composite-error"));
    fireEvent.click(screen.getByTestId("ticker-clear"));
    await waitFor(() => expect(screen.queryByTestId("composite-error")).not.toBeInTheDocument());
  });

  it("clears ticker when clear button clicked", async () => {
    render(<CompositePage token="tok" />);
    fireEvent.change(screen.getByTestId("ticker-input"), { target: { value: "SPY" } });
    fireEvent.click(screen.getByTestId("ticker-submit"));
    await waitFor(() => screen.getByTestId("ticker-clear"));
    fireEvent.click(screen.getByTestId("ticker-clear"));
    expect(screen.getByText(/Enter a ticker above/i)).toBeInTheDocument();
  });

  it("does not call getComposite when token is null", async () => {
    render(<CompositePage token={null} />);
    fireEvent.change(screen.getByTestId("ticker-input"), { target: { value: "SPY" } });
    fireEvent.click(screen.getByTestId("ticker-submit"));
    await waitFor(() => expect(mockGetComposite).not.toHaveBeenCalled());
  });
});
