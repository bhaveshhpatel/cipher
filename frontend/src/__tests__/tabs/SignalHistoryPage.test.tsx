import React from "react";
import { render, screen } from "@testing-library/react";

jest.mock("@/components/dashboard/SignalHistory", () => ({
  SignalHistory: ({ token }: { token: string | null }) => (
    <div data-testid="signal-history" data-token={token ?? "null"} />
  ),
}));

import { SignalHistoryPage } from "../../app/dashboard/_tabs/SignalHistoryPage";

describe("SignalHistoryPage", () => {
  it("renders header", () => {
    render(<SignalHistoryPage token="tok" />);
    expect(screen.getByText("Signal History")).toBeInTheDocument();
  });

  it("renders subheading with formula", () => {
    render(<SignalHistoryPage token="tok" />);
    expect(screen.getByText(/flow.*0\.55/i)).toBeInTheDocument();
  });

  it("renders SignalHistory with token", () => {
    render(<SignalHistoryPage token="tok" />);
    expect(screen.getByTestId("signal-history")).toHaveAttribute("data-token", "tok");
  });

  it("passes null token through", () => {
    render(<SignalHistoryPage token={null} />);
    expect(screen.getByTestId("signal-history")).toHaveAttribute("data-token", "null");
  });
});
