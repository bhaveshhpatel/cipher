/**
 * Regression tests for app/(auth)/login/page.tsx
 *
 * Covers:
 *   - Page renders the Sign in heading and email/password fields
 *   - Form submit calls authAPI.login with correct credentials
 *   - Successful login stores token in localStorage and redirects to /dashboard
 *   - Successful login also fetches /me and stores email + role
 *   - Failed login (API throws) shows an error message
 *   - Loading state disables the submit button during in-flight request
 *   - "No account?" link points to /register
 */
import React from "react";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// ── mock next/navigation ──────────────────────────────────────────────────────
const mockPush    = jest.fn();
const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
}));

// ── mock next/link ─────────────────────────────────────────────────────────────
jest.mock("next/link", () =>
  function Link({ href, children }: { href: string; children: React.ReactNode }) {
    return <a href={href}>{children}</a>;
  }
);

// ── mock authAPI ──────────────────────────────────────────────────────────────
const mockLogin = jest.fn();
jest.mock("@/lib/api", () => ({
  authAPI: { login: (...args: unknown[]) => mockLogin(...args) },
}));

// ── mock fetch for /me call ───────────────────────────────────────────────────
const FAKE_TOKEN = "fake.jwt.token";
const FAKE_EMAIL = "trader@cipher.io";
const FAKE_ROLE  = "user";

function mockFetchMe(ok = true) {
  global.fetch = jest.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 401,
    json: () => Promise.resolve(ok ? { email: FAKE_EMAIL, role: FAKE_ROLE } : {}),
  } as Response);
}

import LoginPage from "../src/app/(auth)/login/page";

// ── suite ─────────────────────────────────────────────────────────────────────

describe("LoginPage", () => {
  beforeEach(() => {
    localStorage.clear();
    jest.clearAllMocks();
    mockFetchMe();
  });

  it("renders the sign-in heading", () => {
    render(<LoginPage />);
    expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });

  it("renders email and password inputs", () => {
    render(<LoginPage />);
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
  });

  it("renders a link to /register", () => {
    render(<LoginPage />);
    const link = screen.getByRole("link", { name: /create one/i });
    expect(link).toHaveAttribute("href", "/register");
  });

  it("calls authAPI.login with entered credentials on submit", async () => {
    mockLogin.mockResolvedValue({ access_token: FAKE_TOKEN });
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText(/email/i), FAKE_EMAIL);
    await userEvent.type(screen.getByLabelText(/password/i), "password123");
    fireEvent.submit(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith(FAKE_EMAIL, "password123"));
  });

  it("stores token in localStorage and redirects to /dashboard on success", async () => {
    mockLogin.mockResolvedValue({ access_token: FAKE_TOKEN });
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText(/email/i), FAKE_EMAIL);
    await userEvent.type(screen.getByLabelText(/password/i), "password123");
    fireEvent.submit(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(localStorage.getItem("cipher_token")).toBe(FAKE_TOKEN));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/dashboard"));
  });

  it("stores email and role from /me after successful login", async () => {
    mockLogin.mockResolvedValue({ access_token: FAKE_TOKEN });
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText(/email/i), FAKE_EMAIL);
    await userEvent.type(screen.getByLabelText(/password/i), "password123");
    fireEvent.submit(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(localStorage.getItem("cipher_email")).toBe(FAKE_EMAIL));
    await waitFor(() => expect(localStorage.getItem("cipher_role")).toBe(FAKE_ROLE));
  });

  it("shows error message when login fails", async () => {
    mockLogin.mockRejectedValue(new Error("Invalid credentials"));
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText(/email/i), "wrong@email.com");
    await userEvent.type(screen.getByLabelText(/password/i), "badpassword");
    fireEvent.submit(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() =>
      expect(screen.getByText(/invalid credentials/i)).toBeInTheDocument()
    );
    expect(localStorage.getItem("cipher_token")).toBeNull();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("shows generic fallback error when login throws a non-Error", async () => {
    mockLogin.mockRejectedValue("network down");
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText(/email/i), "a@b.com");
    await userEvent.type(screen.getByLabelText(/password/i), "pw");
    fireEvent.submit(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() =>
      expect(screen.getByText(/login failed/i)).toBeInTheDocument()
    );
  });

  it("disables the submit button while loading", async () => {
    let resolveLogin!: (v: unknown) => void;
    mockLogin.mockReturnValue(new Promise(res => { resolveLogin = res; }));
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText(/email/i), FAKE_EMAIL);
    await userEvent.type(screen.getByLabelText(/password/i), "password123");
    fireEvent.submit(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /signing in/i })).toBeDisabled()
    );

    act(() => resolveLogin({ access_token: FAKE_TOKEN }));
  });
});
