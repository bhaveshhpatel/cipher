/**
 * Regression tests for app/dashboard/page.tsx
 *
 * Covers:
 *   - Auth guard: unauthenticated user (ready=true, isAuthenticated=false)
 *     → router.replace('/') is called and nothing renders
 *   - Auth guard: page does NOT redirect while ready=false (loading flicker prevention)
 *   - Authenticated user: dashboard renders without redirect
 *   - Authenticated user: email is displayed in the header
 *   - Authenticated user: Sign out button is present and calls logout()
 *   - Tab navigation: all 5 tab labels are rendered
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// ── mock next/navigation ──────────────────────────────────────────────────────
const mockPush    = jest.fn();
const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
}));

// ── mock all hooks that make network calls ─────────────────────────────────────
const mockLogout = jest.fn();

const defaultAuthState = {
  token:           "fake.jwt.token",
  email:           "trader@cipher.io",
  isAuthenticated: true,
  ready:           true,
  logout:          mockLogout,
};

jest.mock("@/hooks/useAuth", () => ({
  useAuth: () => defaultAuthState,
}));

jest.mock("@/hooks/useFlow", () => ({
  useFlow: () => ({ events: [], loading: false, error: null, fetch: jest.fn() }),
}));

jest.mock("@/hooks/useSimulation", () => ({
  useSimulation: () => ({ result: null, loading: false, error: null, progress: 0, run: jest.fn() }),
}));

jest.mock("@/hooks/useSignalStream", () => ({
  useSignalStream: () => ({ signals: [], connected: false }),
}));

jest.mock("@/lib/api", () => ({
  api: {
    getStats:     jest.fn().mockResolvedValue({ stats: null }),
    getComposite: jest.fn().mockResolvedValue(null),
  },
}));

// ── mock all dashboard sub-components to avoid deep render trees ──────────────
jest.mock("@/components/CipherLogo",             () => ({ CipherLogo: () => <span>LOGO</span> }));
jest.mock("@/components/ThemeToggle",            () => ({ ThemeToggle: () => <button>Theme</button> }));
jest.mock("@/components/dashboard/StreamStatsBar", () => ({ StreamStatsBar: () => null }));
jest.mock("@/components/dashboard/FlowTable",    () => ({ FlowTable: () => <div data-testid="flow-table" /> }));
jest.mock("@/components/dashboard/SignalFeed",   () => ({ SignalFeed: () => <div data-testid="signal-feed" /> }));
jest.mock("@/components/dashboard/SimulationPanel", () => ({ SimulationPanel: () => <div data-testid="sim-panel" /> }));
jest.mock("@/components/dashboard/CompositeCard",() => ({ CompositeCard: () => <div data-testid="composite-card" /> }));
jest.mock("@/components/dashboard/SignalHistory",() => ({ SignalHistory: () => <div data-testid="signal-history" /> }));

import DashboardPage from "../src/app/dashboard/page";

// ── helper: override useAuth return value for one test ─────────────────────────
function withAuthState(overrides: Partial<typeof defaultAuthState>) {
  const { useAuth } = require("@/hooks/useAuth");
  (useAuth as jest.Mock).mockReturnValueOnce({ ...defaultAuthState, ...overrides });
}

// ── suite ─────────────────────────────────────────────────────────────────────

describe("DashboardPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    const { useAuth } = require("@/hooks/useAuth");
    (useAuth as jest.Mock).mockReturnValue(defaultAuthState);
  });

  // ── auth guard ──────────────────────────────────────────────────────────────

  it("[AUTH GUARD] redirects to / when not authenticated and ready", async () => {
    withAuthState({ isAuthenticated: false, ready: true, token: null });
    render(<DashboardPage />);
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/"));
  });

  it("[AUTH GUARD] does NOT redirect while ready=false (prevents flicker)", () => {
    withAuthState({ isAuthenticated: false, ready: false, token: null });
    render(<DashboardPage />);
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("[AUTH GUARD] does NOT redirect when authenticated", async () => {
    render(<DashboardPage />);
    await waitFor(() => expect(screen.queryByTestId("flow-table")).toBeInTheDocument());
    expect(mockReplace).not.toHaveBeenCalled();
  });

  // ── render ──────────────────────────────────────────────────────────────────

  it("renders the user email in the header", async () => {
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText("trader@cipher.io")).toBeInTheDocument());
  });

  it("renders the Sign out button", async () => {
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument());
  });

  it("calls logout() when Sign out is clicked", async () => {
    render(<DashboardPage />);
    const btn = await screen.findByRole("button", { name: /sign out/i });
    fireEvent.click(btn);
    expect(mockLogout).toHaveBeenCalledTimes(1);
  });

  // ── tab navigation ──────────────────────────────────────────────────────────

  it("renders all 5 tab labels", async () => {
    render(<DashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /flow scanner/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /live signals/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /ai simulation/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /composite/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /signal history/i })).toBeInTheDocument();
    });
  });

  it("shows FlowTable by default (flow tab is active)", async () => {
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("flow-table")).toBeInTheDocument());
  });

  it("switches to SignalFeed when Live Signals tab is clicked", async () => {
    render(<DashboardPage />);
    const signalsTab = await screen.findByRole("button", { name: /live signals/i });
    fireEvent.click(signalsTab);
    await waitFor(() => expect(screen.getByTestId("signal-feed")).toBeInTheDocument());
  });
});
