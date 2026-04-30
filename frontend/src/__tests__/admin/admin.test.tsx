/**
 * admin.test.tsx
 * Page-level tests: auth redirects, card presence.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

// ── Mocks ─────────────────────────────────────────────────
const mockReplace = vi.fn();
const mockPush    = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: mockPush }),
}));

const mockUseAuth     = vi.fn();
const mockUseAdminDemo = vi.fn();
vi.mock("@/hooks/useAuth",      () => ({ useAuth:      (...a: unknown[]) => mockUseAuth(...a) }));
vi.mock("@/hooks/useAdminDemo", () => ({ useAdminDemo: (...a: unknown[]) => mockUseAdminDemo(...a) }));

// Stub all cards so page renders fast
vi.mock("../_cards/DemoEngineCard",      () => ({ DemoEngineCard:      () => <div data-testid="card-demo" /> }));
vi.mock("../_cards/StreamHealthCard",    () => ({ StreamHealthCard:    () => <div data-testid="card-stream" /> }));
vi.mock("../_cards/TierThresholdsCard",  () => ({ TierThresholdsCard:  () => <div data-testid="card-tier-thresh" /> }));
vi.mock("../_cards/IngestionConfigCard", () => ({ IngestionConfigCard: () => <div data-testid="card-ingestion" /> }));
vi.mock("../_cards/HowItWorksCard",      () => ({ HowItWorksCard:      () => <div data-testid="card-how" /> }));
vi.mock("../_cards/TierDistributionCard",() => ({ TierDistributionCard:() => <div data-testid="card-tier-dist" /> }));
vi.mock("../_cards/ActivityLogCard",     () => ({ ActivityLogCard:     () => <div data-testid="card-activity" /> }));

import AdminPage from "../../app/admin/page";

const DEMO_DEFAULTS = {
  status: null, isRunning: false, loading: false, error: null, toggle: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAdminDemo.mockReturnValue(DEMO_DEFAULTS);
});

describe("AdminPage — auth guards", () => {
  it("returns null while auth is not ready", () => {
    mockUseAuth.mockReturnValue({ token: null, email: null, isAdmin: false, isAuthenticated: false, ready: false });
    const { container } = render(<AdminPage />);
    expect(container.firstChild).toBeNull();
  });

  it("redirects to / when not authenticated", () => {
    mockUseAuth.mockReturnValue({ token: null, email: null, isAdmin: false, isAuthenticated: false, ready: true });
    render(<AdminPage />);
    expect(mockReplace).toHaveBeenCalledWith("/");
  });

  it("redirects to /dashboard when authenticated but not admin", () => {
    mockUseAuth.mockReturnValue({ token: "tok", email: "u@x.com", isAdmin: false, isAuthenticated: true, ready: true });
    render(<AdminPage />);
    expect(mockReplace).toHaveBeenCalledWith("/dashboard");
  });

  it("renders null when isAdmin is false even after ready", () => {
    mockUseAuth.mockReturnValue({ token: "tok", email: "u@x.com", isAdmin: false, isAuthenticated: true, ready: true });
    const { container } = render(<AdminPage />);
    expect(container.querySelector("[data-testid='card-demo']")).toBeNull();
  });
});

describe("AdminPage — layout for admin user", () => {
  beforeEach(() => {
    mockUseAuth.mockReturnValue({ token: "tok", email: "admin@cipher.io", isAdmin: true, isAuthenticated: true, ready: true });
  });

  it("renders the top bar with ADMIN badge and title", () => {
    render(<AdminPage />);
    expect(screen.getByText("ADMIN")).toBeInTheDocument();
    expect(screen.getByText("Cipher Control Panel")).toBeInTheDocument();
  });

  it("shows admin email in top bar", () => {
    render(<AdminPage />);
    expect(screen.getByText("admin@cipher.io")).toBeInTheDocument();
  });

  it("renders all 7 cards", () => {
    render(<AdminPage />);
    expect(screen.getByTestId("card-demo")).toBeInTheDocument();
    expect(screen.getByTestId("card-stream")).toBeInTheDocument();
    expect(screen.getByTestId("card-tier-thresh")).toBeInTheDocument();
    expect(screen.getByTestId("card-ingestion")).toBeInTheDocument();
    expect(screen.getByTestId("card-how")).toBeInTheDocument();
    expect(screen.getByTestId("card-tier-dist")).toBeInTheDocument();
    expect(screen.getByTestId("card-activity")).toBeInTheDocument();
  });

  it("← Dashboard button navigates to /dashboard", () => {
    render(<AdminPage />);
    screen.getByText("← Dashboard").click();
    expect(mockPush).toHaveBeenCalledWith("/dashboard");
  });
});
