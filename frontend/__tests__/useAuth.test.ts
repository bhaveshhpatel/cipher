/**
 * Tests for the useAuth hook.
 *
 * Key scenario covered by this suite:
 *   - Ghost session bug: token in localStorage but expired on server.
 *     fetchMe() must treat HTTP 401 as a hard logout so the dashboard
 *     auth guard redirects to login instead of rendering with a dead token.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { useAuth } from "../src/hooks/useAuth";

// ─── helpers ──────────────────────────────────────────────────────────────────

const FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.fake.sig";
const FAKE_EMAIL = "trader@cipher.io";
const FAKE_ROLE  = "user";

function seedStorage(token = FAKE_TOKEN, email = FAKE_EMAIL, role = FAKE_ROLE) {
  localStorage.setItem("cipher_token", token);
  localStorage.setItem("cipher_email", email);
  localStorage.setItem("cipher_role",  role);
}

function mockFetch(status: number, body: unknown = {}) {
  global.fetch = jest.fn().mockResolvedValue({
    ok:     status >= 200 && status < 300,
    status,
    json:   () => Promise.resolve(body),
  } as Response);
}

// ─── suite ────────────────────────────────────────────────────────────────────

describe("useAuth", () => {
  beforeEach(() => {
    localStorage.clear();
    jest.resetAllMocks();
  });

  // ── initial state ──────────────────────────────────────────────────────────

  it("starts unauthenticated when localStorage is empty", async () => {
    mockFetch(200, {});
    const { result } = renderHook(() => useAuth());
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.token).toBeNull();
  });

  it("starts authenticated (optimistic) when a token exists in localStorage", async () => {
    seedStorage();
    mockFetch(200, { email: FAKE_EMAIL, role: FAKE_ROLE });
    const { result } = renderHook(() => useAuth());
    // ready + isAuthenticated should be true synchronously (before fetchMe resolves)
    expect(result.current.ready).toBe(true);
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.token).toBe(FAKE_TOKEN);
  });

  // ── ghost-session / expired token fix ─────────────────────────────────────

  it("[BUG FIX] auto-logs-out when /me returns 401 (expired token)", async () => {
    seedStorage();
    mockFetch(401);

    const { result } = renderHook(() => useAuth());

    // Initially optimistic
    expect(result.current.isAuthenticated).toBe(true);

    // After fetchMe resolves with 401 → must be logged out
    await waitFor(() => expect(result.current.isAuthenticated).toBe(false));

    expect(result.current.token).toBeNull();
    expect(result.current.email).toBeNull();
    expect(result.current.ready).toBe(true);

    // Storage must be cleared
    expect(localStorage.getItem("cipher_token")).toBeNull();
    expect(localStorage.getItem("cipher_email")).toBeNull();
    expect(localStorage.getItem("cipher_role")).toBeNull();
  });

  it("keeps session alive when /me returns a non-401 error (network blip)", async () => {
    seedStorage();
    mockFetch(503);

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.ready).toBe(true));

    // Non-401 errors must not log the user out
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.token).toBe(FAKE_TOKEN);
    expect(localStorage.getItem("cipher_token")).toBe(FAKE_TOKEN);
  });

  it("keeps session alive when /me throws a network exception", async () => {
    seedStorage();
    global.fetch = jest.fn().mockRejectedValue(new Error("Network error"));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.ready).toBe(true));

    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.token).toBe(FAKE_TOKEN);
  });

  // ── login ──────────────────────────────────────────────────────────────────

  it("login: sets authenticated state and persists to localStorage on success", async () => {
    global.fetch = jest.fn()
      // First call: POST /api/auth/token
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve({ access_token: FAKE_TOKEN }),
      } as Response)
      // Second call: GET /api/auth/me
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve({ email: FAKE_EMAIL, role: FAKE_ROLE }),
      } as Response);

    const { result } = renderHook(() => useAuth());

    await act(async () => {
      await result.current.login(FAKE_EMAIL, "password123");
    });

    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.token).toBe(FAKE_TOKEN);
    expect(result.current.email).toBe(FAKE_EMAIL);
    expect(localStorage.getItem("cipher_token")).toBe(FAKE_TOKEN);
  });

  it("login: sets error state on bad credentials (401)", async () => {
    global.fetch = jest.fn().mockResolvedValueOnce({
      ok: false, status: 401,
      json: () => Promise.resolve({ detail: "Invalid credentials" }),
    } as Response);

    const { result } = renderHook(() => useAuth());

    await act(async () => {
      await result.current.login("wrong@email.com", "badpassword");
    });

    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.error).toBe("Invalid credentials");
    expect(result.current.loading).toBe(false);
  });

  // ── logout ─────────────────────────────────────────────────────────────────

  it("logout: clears state and localStorage", async () => {
    seedStorage();
    mockFetch(200, { email: FAKE_EMAIL, role: FAKE_ROLE });

    const { result } = renderHook(() => useAuth());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => result.current.logout());

    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.token).toBeNull();
    expect(result.current.email).toBeNull();
    expect(localStorage.getItem("cipher_token")).toBeNull();
  });
});
