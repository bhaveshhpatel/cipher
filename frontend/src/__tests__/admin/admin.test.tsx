/**
 * admin.test.tsx
 * Page-level tests: auth redirects, card presence.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

// ── next/navigation ───────────────────────────────────────
jest.mock("next/navigation", () => ({ useRouter: jest.fn() }));

// ── Card stubs (paths relative to this test file) ──────────────
jest.mock("../../app/admin/_cards/DemoEngineCard",       () => ({ DemoEngineCard:       () => <div data-testid="card-demo" /> }));
jest.mock("../../app/admin/_cards/StreamHealthCard",     () => ({ StreamHealthCard:     () => <div data-testid="card-stream" /> }));
jest.mock("../../app/admin/_cards/TierThresholdsCard",   () => ({ TierThresholdsCard:   () => <div data-testid="card-tier-thresh" /> }));
jest.mock("../../app/admin/_cards/IngestionConfigCard",  () => ({ IngestionConfigCard:  () => <div data-testid="card-ingestion" /> }));
jest.mock("../../app/admin/_cards/HowItWorksCard",       () => ({ HowItWorksCard:       () => <div data-testid="card-how" /> }));
jest.mock("../../app/admin/_cards/TierDistributionCard", () => ({ TierDistributionCard: () => <div data-testid="card-tier-dist" /> }));
jest.mock("../../app/admin/_cards/ActivityLogCard",      () => ({ ActivityLogCard:      () => <div data-testid="card-activity" /> }));

// ── Hook auto-mocks ─────────────────────────────────────
jest.mock("@/hooks/useAuth");
jest.mock("@/hooks/useAdminDemo");

import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useAdminDemo } from "@/hooks/useAdminDemo";
import AdminPage from "../../app/admin/page";

const mockUseRouter    = useRouter    as jest.Mock;
const mockUseAuth      = useAuth      as jest.Mock;
const mockUseAdminDemo = useAdminDemo as jest.Mock;

const mockReplace = jest.fn();
const mockPush    = jest.fn();

beforeEach(() => {
  jest.clearAllMocks();
  mockUseRouter.mockReturnValue({ replace: mockReplace, push: mockPush });
  mockUseAdminDemo.mockReturnValue({
    status: null, isRunning: false, loading: false, error: null, toggle: jest.fn(),
  });
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

  it("returns null when isAdmin is false even after ready", () => {
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
