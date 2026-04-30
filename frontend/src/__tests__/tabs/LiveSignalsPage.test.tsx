import React from "react";
import { render, screen } from "@testing-library/react";

jest.mock("@/components/dashboard/SignalFeed", () => ({
  SignalFeed: ({ connected }: { connected: boolean }) => (
    <div data-testid="signal-feed" data-connected={String(connected)} />
  ),
}));

import { LiveSignalsPage } from "../../app/dashboard/_tabs/LiveSignalsPage";

describe("LiveSignalsPage", () => {
  it("renders header", () => {
    render(<LiveSignalsPage signals={[]} connected={true} token="tok" />);
    expect(screen.getByText("Live Signal Feed")).toBeInTheDocument();
  });

  it("shows connected copy when connected=true", () => {
    render(<LiveSignalsPage signals={[]} connected={true} token="tok" />);
    expect(screen.getByText(/WebSocket connected/i)).toBeInTheDocument();
  });

  it("shows connecting copy when connected=false", () => {
    render(<LiveSignalsPage signals={[]} connected={false} token="tok" />);
    expect(screen.getByText(/Connecting to stream/i)).toBeInTheDocument();
  });

  it("renders SignalFeed", () => {
    render(<LiveSignalsPage signals={[]} connected={true} token="tok" />);
    expect(screen.getByTestId("signal-feed")).toBeInTheDocument();
  });

  it("passes connected prop to SignalFeed", () => {
    render(<LiveSignalsPage signals={[]} connected={false} token="tok" />);
    expect(screen.getByTestId("signal-feed")).toHaveAttribute("data-connected", "false");
  });
});
