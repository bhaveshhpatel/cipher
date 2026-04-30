/**
 * dashboard.test.tsx
 * Regression suite for DashboardPage
 * - All 7 tabs render without crashing
 * - Tab switching works
 * - Auth guard redirects unauthenticated users
 * - Signals badge renders when signals exist
 *
 * NOTE on `within(sidebar)` scoping:
 *   DashboardLayout renders SidebarNav (hidden md:flex) AND MobileTabBar
 *   (flex md:hidden). In jsdom, Tailwind visibility classes are not evaluated,
 *   so both navs are present in the DOM simultaneously. Every tab button and
 *   badge span therefore appears twice. We scope all tab/badge queries to
 *   data-testid="sidebar-nav" via `within()` to avoid "Found multiple elements".
 *
 * NOTE on tab visibility strategy:
 *   DashboardPage uses a visited-set + CSS-hide pattern. Once a tab is first
 *   visited its component stays mounted (so filter/ticker/sim state persists).
 *   Inactive tabs are wrapped in a <div style="display:none"> rather than
 *   being unmounted. Tests must assert display:none / no display:none rather
 *   than presence/absence in the DOM.
 */

import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { useRouter } from "next/navigation";
import DashboardPage from "../app/dashboard/page";

// ─── Mock next/navigation ────────────────────────────────────────────────
jest.mock("next/navigation", () => ({
  useRouter: jest.fn(),
}));

const mockReplace = jest.fn();
beforeEach(() => {
  (useRouter as jest.Mock).mockReturnValue({ replace: mockReplace });
});

// ─── Mock hooks ─────────────────────────────────────────────────────────────────
jest.mock("@/hooks/useAuth",         () => ({ useAuth:         jest.fn() }));
jest.mock("@/hooks/useFlow",         () => ({ useFlow:         jest.fn() }));
jest.mock("@/hooks/useSimulation",   () => ({ useSimulation:   jest.fn() }));
jest.mock("@/hooks/useSignalStream", () => ({ useSignalStream: jest.fn() }));
jest.mock("@/hooks/useFlowEvents",   () => ({ useFlowEvents:   jest.fn() }));
jest.mock("@/hooks/useFlowEpisodes", () => ({ useFlowEpisodes: jest.fn() }));

// ─── Mock API ─────────────────────────────────────────────────────────────────────
jest.mock("@/lib/api", () => ({
  api: {
    getStats:     jest.fn().mockResolvedValue({ stats: { symbols_tracked: 100, events_per_second: 5, active_episodes: 3, uptime_seconds: 3600 } }),
    getComposite: jest.fn().mockResolvedValue(null),
  },
}));

// ─── Mock components ──────────────────────────────────────────────────────────────
jest.mock("@/components/CipherLogo",                () => ({ CipherLogo:       () => <div data-testid="cipher-logo" /> }));
jest.mock("@/components/ThemeToggle",               () => ({ ThemeToggle:       () => <div data-testid="theme-toggle" /> }));
jest.mock("@/components/dashboard/StreamStatsBar",  () => ({ StreamStatsBar:    () => <div data-testid="stream-stats-bar" /> }));
jest.mock("@/components/dashboard/FlowTable",       () => ({ FlowTable:         () => <div data-testid="flow-table" /> }));
jest.mock("@/components/dashboard/SignalFeed",      () => ({ SignalFeed:        () => <div data-testid="signal-feed" /> }));
jest.mock("@/components/dashboard/SimulationPanel", () => ({ SimulationPanel:   () => <div data-testid="simulation-panel" /> }));
jest.mock("@/components/dashboard/CompositeCard",   () => ({ CompositeCard:     () => <div data-testid="composite-card" /> }));
jest.mock("@/components/dashboard/SignalHistory",   () => ({ SignalHistory:     () => <div data-testid="signal-history" /> }));
jest.mock("@/components/dashboard/FlowEventsTab",   () => ({ FlowEventsTab:     () => <div data-testid="flow-events-tab" /> }));
jest.mock("@/components/dashboard/FlowEpisodesTab",  () => ({ FlowEpisodesTab:   () => <div data-testid="flow-episodes-tab" /> }));

// ─── Import mocked hooks (after jest.mock hoisting) ─────────────────────────────
import { useAuth }         from "@/hooks/useAuth";
import { useFlow }         from "@/hooks/useFlow";
import { useSimulation }   from "@/hooks/useSimulation";
import { useSignalStream } from "@/hooks/useSignalStream";
import { useFlowEvents }   from "@/hooks/useFlowEvents";
import { useFlowEpisodes } from "@/hooks/useFlowEpisodes";

// ─── Default hook return values ─────────────────────────────────────────────────────
const AUTHED = {
  token:           "test-token",
  email:           "test@example.com",
  isAuthenticated: true,
  ready:           true,
  logout:          jest.fn(),
};
const UNAUTHED  = { token: null, email: null, isAuthenticated: false, ready: true,  logout: jest.fn() };
const NOT_READY = { token: null, email: null, isAuthenticated: false, ready: false, logout: jest.fn() };

const DEFAULT_FLOW         = { events: [],   loading: false, error: null, fetch: jest.fn() };
const DEFAULT_SIM          = { result: null, loading: false, error: null, progress: 0, run: jest.fn() };
const DEFAULT_SIGNAL_STREAM = { signals: [], connected: true };
const DEFAULT_FLOW_EVENTS   = { events: [],   loading: false, error: null, fetch: jest.fn() };
const DEFAULT_FLOW_EPISODES = { episodes: [], loading: false, error: null, fetch: jest.fn() };

function setupMocks(overrides: { auth?: object; signals?: object } = {}) {
  (useAuth         as jest.Mock).mockReturnValue({ ...AUTHED,                ...overrides.auth    });
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

// ─── Helpers ─────────────────────────────────────────────────────────────────────
/**
 * Returns the closest ancestor wrapper div that DashboardPage injects for
 * CSS-hide tab management. We check its style.display to distinguish
 * "active" (no display override) from "hidden" (display:none).
 */
function getTabWrapper(el: HTMLElement): HTMLElement {
  // DashboardPage wraps each tab in a plain <div key={t}> one level above
  // the component root. Walk up until we reach it.
  let node: HTMLElement | null = el;
  while (node && node.parentElement && node.parentElement.getAttribute("data-testid") !== "dashboard-content") {
    const parent = node.parentElement;
    // The wrapper div has no attributes — it's a bare <div style?>
    if (parent && !parent.hasAttribute("data-testid") && !parent.hasAttribute("class")) {
      return parent as HTMLElement;
    }
    node = parent as HTMLElement;
  }
  // Fallback: return the element's direct parent
  return el.parentElement as HTMLElement ?? el;
}

function isTabVisible(testId: string): boolean {
  const el = screen.getByTestId(testId);
  // Walk up to the CSS-hide wrapper div
  let node: HTMLElement | null = el.parentElement;
  while (node) {
    if (node.style && node.style.display === "none") return false;
    // Stop at the DashboardLayout children boundary (arbitrary depth limit)
    if (node === document.body) break;
    node = node.parentElement;
  }
  return true;
}

// ─── Auth guard ───────────────────────────────────────────────────────────────────
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

// ─── Tab nav renders all tabs ───────────────────────────────────────────────────────────
describe("Tab navigation — all tabs exist", () => {
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
      const sidebar = screen.getByTestId("sidebar-nav");
      expect(within(sidebar).getByRole("button", { name: new RegExp(label, "i") })).toBeInTheDocument();
    });
  });

  test("renders tab button: Flow Events (scoped to nav button)", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    const buttons = within(sidebar).getAllByRole("button", { name: /flow events/i });
    expect(buttons.length).toBeGreaterThanOrEqual(1);
  });
});

// ─── Tab switching ────────────────────────────────────────────────────────────────────
describe("Tab switching", () => {
  test("default tab is Flow Events — FlowEventsTab is visible", () => {
    render(<DashboardPage />);
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
    expect(isTabVisible("flow-events-tab")).toBe(true);
  });

  test("clicking Live Signals renders SignalFeed", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    fireEvent.click(within(sidebar).getByRole("button", { name: /live signals/i }));
    expect(screen.getByTestId("signal-feed")).toBeInTheDocument();
    expect(isTabVisible("signal-feed")).toBe(true);
  });

  test("clicking AI Simulation renders SimulationPanel", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    fireEvent.click(within(sidebar).getByRole("button", { name: /ai simulation/i }));
    expect(screen.getByTestId("simulation-panel")).toBeInTheDocument();
    expect(isTabVisible("simulation-panel")).toBe(true);
  });

  test("clicking Composite renders CompositeCard", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    fireEvent.click(within(sidebar).getByRole("button", { name: /composite/i }));
    expect(screen.getByTestId("composite-card")).toBeInTheDocument();
    expect(isTabVisible("composite-card")).toBe(true);
  });

  test("clicking Signal History renders SignalHistory", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    fireEvent.click(within(sidebar).getByRole("button", { name: /signal history/i }));
    expect(screen.getByTestId("signal-history")).toBeInTheDocument();
    expect(isTabVisible("signal-history")).toBe(true);
  });

  test("clicking Flow Events tab renders FlowEventsTab", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    fireEvent.click(within(sidebar).getByRole("button", { name: /flow events/i }));
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
    expect(isTabVisible("flow-events-tab")).toBe(true);
  });

  test("clicking Episodes renders FlowEpisodesTab", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    fireEvent.click(within(sidebar).getByRole("button", { name: /episodes/i }));
    expect(screen.getByTestId("flow-episodes-tab")).toBeInTheDocument();
    expect(isTabVisible("flow-episodes-tab")).toBe(true);
  });

  /**
   * CSS-hide tab visibility:
   * After switching tabs the previously-active tab stays in the DOM
   * (so its state is preserved) but its wrapper is set to display:none.
   * We verify:
   *   1. The old tab is still in the DOM (mounted, state intact)
   *   2. Its wrapper has display:none (not user-visible)
   *   3. The new tab is visible (no display:none ancestor)
   */
  test("only one tab panel is visible at a time (CSS-hide, not unmount)", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");

    // Default: Flow Events active
    expect(isTabVisible("flow-events-tab")).toBe(true);

    // Switch to Live Signals
    fireEvent.click(within(sidebar).getByRole("button", { name: /live signals/i }));

    // flow-events-tab stays in DOM but is now hidden
    expect(screen.getByTestId("flow-events-tab")).toBeInTheDocument();
    expect(isTabVisible("flow-events-tab")).toBe(false);

    // signal-feed is now visible
    expect(screen.getByTestId("signal-feed")).toBeInTheDocument();
    expect(isTabVisible("signal-feed")).toBe(true);
  });

  test("can switch back to Flow Events after visiting another tab", () => {
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    fireEvent.click(within(sidebar).getByRole("button", { name: /episodes/i }));
    expect(isTabVisible("flow-episodes-tab")).toBe(true);
    expect(isTabVisible("flow-events-tab")).toBe(false);
    fireEvent.click(within(sidebar).getByRole("button", { name: /flow events/i }));
    expect(isTabVisible("flow-events-tab")).toBe(true);
    expect(isTabVisible("flow-episodes-tab")).toBe(false);
  });

  /**
   * Tabs not yet visited must not be in the DOM at all (lazy mount).
   * This validates the visited-set guards against unnecessary hook calls on load.
   */
  test("unvisited tabs are not mounted on initial render", () => {
    render(<DashboardPage />);
    // Only flow_events is in visited on mount — these shouldn't exist yet
    expect(screen.queryByTestId("signal-feed")).not.toBeInTheDocument();
    expect(screen.queryByTestId("simulation-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("composite-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("signal-history")).not.toBeInTheDocument();
    expect(screen.queryByTestId("flow-episodes-tab")).not.toBeInTheDocument();
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
    const sidebar = screen.getByTestId("sidebar-nav");
    expect(within(sidebar).getByText("5")).toBeInTheDocument();
  });

  test("badge shows 99+ when signals exceed 99", () => {
    setupMocks({ signals: { signals: Array(150).fill({ id: "s1" }), connected: true } });
    render(<DashboardPage />);
    const sidebar = screen.getByTestId("sidebar-nav");
    expect(within(sidebar).getByText("99+")).toBeInTheDocument();
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
