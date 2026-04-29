/**
 * dashboard.test.tsx
 * Regression suite for DashboardPage
 * - All 7 tabs render without crashing
 * - Tab switching works
 * - Auth guard redirects unauthenticated users
 * - Signals badge renders when signals exist
 */

import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useRouter } from "next/navigation";
import DashboardPage from "../app/dashboard/page";

// ─── Mock next/navigation ──────────────────────────────────────────────────
jest.mock("next/navigation", () => ({
  useRouter: jest.fn(),
}));

const mockReplace = jest.fn();
beforeEach(() => {
  (useRouter as jest.Mock).mockReturnValue({ replace: mockReplace });
});

// ─── Mock hooks ───────────────────────────────────────────────────────────────────
jest.mock("@/hooks/useAuth", () => ({
  useAuth: jest.fn(),
}));
jest.mock("@/hooks/useFlow", () => ({
  useFlow: jest.fn(),
}));
jest.mock("@/hooks/useSimulation", () => ({
  useSimulation: jest.fn(),
}));
jest.mock("@/hooks/useSignalStream", () => ({
  useSignalStream: jest.fn(),
}));
jest.mock("@/hooks/useFlowEvents", () => ({
  useFlowEvents: jest.fn(),
}));
jest.mock("@/hooks/useFlowEpisodes", () => ({
  useFlowEpisodes: jest.fn(),
}));

// ─── Mock API ─────────────────────────────────────────────────────────────────────
jest.mock("@/lib/api", () => ({
  api: {
    getStats:    jest.fn().mockResolvedValue({ stats: { symbols_tracked: 100, events_per_second: 5, active_episodes: 3, uptime_seconds: 3600 } }),
    getComposite: jest.fn().mockResolvedValue(null),
  },
}));

// ─── Mock components ─────────────────────────────────────────────────────────────────
jest.mock("@/components/CipherLogo",                       () => ({ CipherLogo: () => <div data-testid="cipher-logo" /> }));
jest.mock("@/components/ThemeToggle",                      () => ({ ThemeToggle: () => <div data-testid="theme-toggle" /> }));
jest.mock("@/components/dashboard/StreamStatsBar",         () => ({ StreamStatsBar: () => <div data-testid="stream-stats-bar" /> }));
jest.mock("@/components/dashboard/FlowTable",              () => ({ FlowTable: () => <div data-testid="flow-table" /> }));
jest.mock("@/components/dashboard/SignalFeed",             () => ({ SignalFeed: () => <div data-testid="signal-feed" /> }));
jest.mock("@/components/dashboard/SimulationPanel",        () => ({ SimulationPanel: () => <div data-testid="simulation-panel" /> }));
jest.mock("@/components/dashboard/CompositeCard",          () => ({ CompositeCard: () => <div data-testid="composite-card" /> }));
jest.mock("@/components/dashboard/SignalHistory",          () => ({ SignalHistory: () => <div data-testid="signal-history" /> }));
jest.mock("@/components/dashboard/FlowEventsTab",          () => ({ FlowEventsTab: () => <div data-testid="flow-events-tab" /> }));
jest.mock("@/components/dashboard/FlowEpisodesTab",        () => ({ FlowEpisodesTab: () => <div data-testid="flow-episodes-tab" /> }));

// ─── Import mocked hooks (after jest.mock hoisting) ───────────────────────────────────────
import { useAuth }          from "@/hooks/useAuth";
import { useFlow }          from "@/hooks/useFlow";
import { useSimulation }    from "@/hooks/useSimulation";
import { useSignalStream }  from "@/hooks/useSignalStream";
import { useFlowEvents }    from "@/hooks/useFlowEvents";
import { useFlowEpisodes }  from "@/hooks/useFlowEpisodes";

// ─── Default hook return values ─────────────────────────────────────────────────────────────
const AUTHED = {
  token: "test-token",
  email: "test@example.com",
  isAuthenticated: true,
  ready: true,
  logout: jest.fn(),
};
const UNAUTHED  = { token: null, email: null, isAuthenticated: false, ready: true,  logout: jest.fn() };
const NOT_READY = { token: null, email: null, isAuthenticated: false, ready: false, logout: jest.fn() };

const DEFAULT_FLOW = {
  events: [],
  loading: false,
  error: null,
  fetch: jest.fn(),
};
const DEFAULT_SIM = {
  result: null,
  loading: false,
  error: null,
  progress: 0,
  run: jest.fn(),
};
const DEFAULT_SIGNAL_STREAM = { signals: [], connected: true };
const DEFAULT_FLOW_EVENTS   = { events: [], loading: false, error: null, fetch: jest.fn() };
const DEFAULT_FLOW_EPISODES = { episodes: [], loading: false, error: null, fetch: jest.fn() };

function setupMocks(overrides: { auth?: object; signals?: object } = {}) {
  (useAuth         as jest.Mock).mockReturnValue({ ...AUTHED,                ...overrides.auth });
  (useFlow         as jest.Mock).mockReturnValue(DEFAULT_FLOW);
  (useSimulation   as jest.Mock).mockReturnValue(DEFAULT_SIM);
  (useSignalStream as jest.Mock).mockReturnValue({ ...DEFAULT_SIGNAL_STREAM, ...overrides.signals });
  (useFlowEvents   as jest.Mock).mockReturnValue(DEFAULT_FLOW_EVENTS);
  (useFlowEpisodes as jest.Mock).mockReturnValue(DEFAULT_FLOW_EPISODES);
}

beforeEach(() => {
  jest.clearAllMocks();
  setupMocks();
});

// ─── Auth guard ─────────────────────────────────────────────────────────────────────
describe("Auth guard", () => {
  test("returns null while auth is not ready (prevents flicker)", () => {
    (useAuth as jest.Mock).mockReturnValue(NOT_READY);
    const { container } = render(<DashboardPage />);
    expect(container.firstChild).toBeNull();
  });

  test("redirects unauthenticated user to /", async () => {
    (useAuth as jest.Mock).mockReturnValue(UNAUTHED);
    render(<DashboardPage />);
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/"));
  });

  test("renders dashboard when authenticated", () => {
    render(<DashboardPage />);
    expect(screen.getByTestId("cipher-logo")).toBeInTheDocument();
  });
});

// ─── Tab nav renders all 7 tabs ─────────────────────────────────────────────────────────────
describe("Tab navigation — all 7 tabs exist", () => {
  // Labels as they actually appear in the rendered nav buttons
  const TAB_LABELS = [
    "Flow Events",
    "Live Signals",
    "AI Simulation",
    "Composite",
    "Signal History",
    "Episodes",
  ];

  TAB_LABELS.forEach(label => {
    test(`renders tab button: ${label}`, () => {
      render(<DashboardPage />);
      expect(screen.getByRole("button", { name: new RegExp(label, "i") })).toBeInTheDocument();
    });
  });

  // "Flow Events" tab label also appears in the page <h1>, so scope to button role
  test("renders tab button: Flow Events (scoped to nav button)", () => {
    render(<DashboardPage />);
    const buttons = screen.getAllByRole("button", { name: /flow events/i });
    expect(buttons.length).toBeGreaterThanOrEqual(1);
  });
});

// ─── Tab switching ────────────────────────────────────────────────────────────────────
describe("Tab switching", () => {
  test("default tab is Flow Events — FlowEventsTab is visible", () => {
    render(<DashboardPage />);
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
  });

  test("clicking Live Signals renders SignalFeed", () => {
    render(<DashboardPage />);
    fireEvent.click(screen.getByRole("button", { name: /live signals/i }));
    expect(screen.getByTestId("signal-feed")).toBeInTheDocument();
  });

  test("clicking AI Simulation renders SimulationPanel", () => {
    render(<DashboardPage />);
    fireEvent.click(screen.getByRole("button", { name: /ai simulation/i }));
    expect(screen.getByTestId("simulation-panel")).toBeInTheDocument();
  });

  test("clicking Composite renders CompositeCard", () => {
    render(<DashboardPage />);
    fireEvent.click(screen.getByRole("button", { name: /^composite$/i }));
    expect(screen.getByTestId("composite-card")).toBeInTheDocument();
  });

  test("clicking Signal History renders SignalHistory", () => {
    render(<DashboardPage />);
    fireEvent.click(screen.getByRole("button", { name: /signal history/i }));
    expect(screen.getByTestId("signal-history")).toBeInTheDocument();
  });

  test("clicking Flow Events tab renders FlowEventsTab", () => {
    render(<DashboardPage />);
    // Use role scoping to avoid collision with the <h1> that also reads "Flow Events"
    fireEvent.click(screen.getByRole("button", { name: /flow events/i }));
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
  });

  test("clicking Episodes renders FlowEpisodesTab", () => {
    render(<DashboardPage />);
    fireEvent.click(screen.getByRole("button", { name: /episodes/i }));
    expect(screen.getByTestId("flow-episodes-tab")).toBeInTheDocument();
  });

  test("only one tab panel is visible at a time", () => {
    render(<DashboardPage />);
    // Default: Flow Events tab
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("signal-feed")).not.toBeInTheDocument();
    // Switch to Live Signals
    fireEvent.click(screen.getByRole("button", { name: /live signals/i }));
    expect(screen.queryByTestId("flow-events-tab")).not.toBeInTheDocument();
    expect(screen.getByTestId("signal-feed")).toBeInTheDocument();
  });

  test("can switch back to Flow Events after visiting another tab", () => {
    render(<DashboardPage />);
    fireEvent.click(screen.getByRole("button", { name: /episodes/i }));
    expect(screen.getByTestId("flow-episodes-tab")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /flow events/i }));
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
  });
});

// ─── Signals badge ────────────────────────────────────────────────────────────────────
describe("Live Signals badge", () => {
  test("badge hidden when no signals", () => {
    render(<DashboardPage />);
    const badgeCandidates = screen.queryAllByText(/^\d+$/);
    expect(badgeCandidates.length).toBe(0);
  });

  test("badge shows count when signals exist", () => {
    setupMocks({ signals: { signals: Array(5).fill({ id: "s1" }), connected: true } });
    render(<DashboardPage />);
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  test("badge shows 99+ when signals exceed 99", () => {
    setupMocks({ signals: { signals: Array(150).fill({ id: "s1" }), connected: true } });
    render(<DashboardPage />);
    expect(screen.getByText("99+")).toBeInTheDocument();
  });
});

// ─── Header elements ──────────────────────────────────────────────────────────────────
describe("Header", () => {
  test("renders CIPHER wordmark", () => {
    render(<DashboardPage />);
    expect(screen.getByText("CIPHER")).toBeInTheDocument();
  });

  test("renders user email in header", () => {
    render(<DashboardPage />);
    expect(screen.getByText("test@example.com")).toBeInTheDocument();
  });

  test("sign out button calls logout", () => {
    render(<DashboardPage />);
    fireEvent.click(screen.getByText("Sign out"));
    expect(AUTHED.logout).toHaveBeenCalledTimes(1);
  });
});
