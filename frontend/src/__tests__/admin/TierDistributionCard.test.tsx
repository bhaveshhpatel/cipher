/**
 * TierDistributionCard.test.tsx — 7 cases
 * Mocks global.fetch — no real network calls.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { TierDistributionCard } from "../../app/admin/_cards/TierDistributionCard";

const MOCK_DISTRIBUTION = {
  snapshot_id: "snap-001",
  total:       350,
  tiers: {
    "1": { count: 50,  samples: [{ symbol: "AAPL", open_interest: 120000 }, { symbol: "SPY", open_interest: 890000 }] },
    "2": { count: 100, samples: [{ symbol: "TSLA", open_interest: 45000  }] },
    "3": { count: 200, samples: [{ symbol: "RIVN", open_interest: null   }] },
  },
};

beforeEach(() => jest.clearAllMocks());

describe("TierDistributionCard — states", () => {
  it("renders title and subtitle", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<TierDistributionCard token="tok" />);
    expect(screen.getByText("Tier Distribution")).toBeInTheDocument();
    expect(screen.getByText(/Symbol counts/)).toBeInTheDocument();
  });

  it("shows Loading… initially", () => {
    global.fetch = jest.fn(() => new Promise(() => {})) as jest.Mock;
    render(<TierDistributionCard token="tok" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("does not fetch when token is null", () => {
    global.fetch = jest.fn() as jest.Mock;
    render(<TierDistributionCard token={null} />);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("shows error when fetch fails", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 500 }) as jest.Mock;
    render(<TierDistributionCard token="tok" />);
    await waitFor(() => expect(screen.getByText(/HTTP 500/)).toBeInTheDocument());
  });

  it("renders all 3 tier badges after fetch", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_DISTRIBUTION }) as jest.Mock;
    render(<TierDistributionCard token="tok" />);
    await waitFor(() => expect(screen.getByText("TIER 1")).toBeInTheDocument());
    expect(screen.getByText("TIER 2")).toBeInTheDocument();
    expect(screen.getByText("TIER 3")).toBeInTheDocument();
  });

  it("renders tier symbol counts", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_DISTRIBUTION }) as jest.Mock;
    render(<TierDistributionCard token="tok" />);
    await waitFor(() => expect(screen.getByText("50 symbols")).toBeInTheDocument());
    expect(screen.getByText("100 symbols")).toBeInTheDocument();
    expect(screen.getByText("200 symbols")).toBeInTheDocument();
  });

  it("renders sample symbols and handles null OI", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_DISTRIBUTION }) as jest.Mock;
    render(<TierDistributionCard token="tok" />);
    await waitFor(() => expect(screen.getByText("AAPL")).toBeInTheDocument());
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(screen.getByText("RIVN")).toBeInTheDocument();
    expect(screen.getByText("OI: —")).toBeInTheDocument();
  });

  it("renders snapshot id and total count", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => MOCK_DISTRIBUTION }) as jest.Mock;
    render(<TierDistributionCard token="tok" />);
    await waitFor(() => expect(screen.getByText("snap-001")).toBeInTheDocument());
    expect(screen.getByText("350")).toBeInTheDocument();
  });
});
