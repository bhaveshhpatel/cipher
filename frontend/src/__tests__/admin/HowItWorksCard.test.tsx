/**
 * HowItWorksCard.test.tsx — 4 cases
 * Purely presentational — no mocks needed.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { HowItWorksCard } from "../../app/admin/_cards/HowItWorksCard";

describe("HowItWorksCard", () => {
  it("renders title", () => {
    render(<HowItWorksCard />);
    expect(screen.getByText("Pipeline Overview")).toBeInTheDocument();
  });

  it("renders subtitle", () => {
    render(<HowItWorksCard />);
    expect(screen.getByText("End-to-end data flow")).toBeInTheDocument();
  });

  it("renders all 5 step numbers", () => {
    render(<HowItWorksCard />);
    ["01", "02", "03", "04", "05"].forEach(n =>
      expect(screen.getByText(n)).toBeInTheDocument()
    );
  });

  it("renders all step labels", () => {
    render(<HowItWorksCard />);
    ["Symbols", "Stream", "Classify", "Signals", "Dashboard"].forEach(label =>
      expect(screen.getByText(label)).toBeInTheDocument()
    );
  });
});
