"use client";
import { useState, useEffect, useCallback } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

interface AuthState {
  token:           string | null;
  email:           string | null;
  role:            string | null;
  isAuthenticated: boolean;
  isAdmin:         boolean;
  ready:           boolean;
  loading:         boolean;
  error:           string | null;
}

const INITIAL_STATE: AuthState = {
  token:           null,
  email:           null,
  role:            null,
  isAuthenticated: false,
  isAdmin:         false,
  ready:           false,
  loading:         false,
  error:           null,
};

function clearStorage() {
  localStorage.removeItem("cipher_token");
  localStorage.removeItem("cipher_email");
  localStorage.removeItem("cipher_role");
}

const LOGGED_OUT_READY: Partial<AuthState> = {
  token: null, email: null, role: null,
  isAuthenticated: false, isAdmin: false, ready: true,
};

export function useAuth() {
  const [state, setState] = useState<AuthState>(INITIAL_STATE);

  /**
   * Validates the token against /api/auth/me.
   *
   * - HTTP 401 → token is expired/invalid → hard logout (clear storage,
   *   set isAuthenticated=false so the dashboard guard redirects to login).
   * - Other non-ok responses → treat as a non-fatal network blip; leave
   *   the cached session intact.
   * - Success → refresh email + role from server.
   */
  const fetchMe = useCallback(async (token: string) => {
    try {
      const res = await fetch(`${API}/api/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (res.status === 401) {
        // Token is expired or revoked — log the user out
        clearStorage();
        setState(s => ({ ...s, ...LOGGED_OUT_READY }));
        return;
      }

      if (!res.ok) {
        // Non-auth server error (e.g. 503 on cold start) — keep cached session
        return;
      }

      const data = await res.json();
      const email = data.email ?? null;
      const role  = data.role  ?? "user";
      if (email) localStorage.setItem("cipher_email", email);
      if (role)  localStorage.setItem("cipher_role",  role);
      setState(s => ({ ...s, email, role, isAdmin: role === "admin" || role === "founder" }));
    } catch {
      // Network error — non-fatal, leave session intact
    }
  }, []);

  // Initialise from localStorage on mount
  useEffect(() => {
    const token = localStorage.getItem("cipher_token");
    const email = localStorage.getItem("cipher_email");
    const role  = localStorage.getItem("cipher_role");

    if (!token) {
      setState(s => ({ ...s, ...LOGGED_OUT_READY }));
      return;
    }

    // Optimistic state from cache — renders dashboard immediately
    setState(s => ({
      ...s,
      token,
      email,
      role,
      isAuthenticated: true,
      isAdmin:         role === "admin" || role === "founder",
      ready:           true,
    }));

    // Async server validation — will auto-logout if token is expired
    fetchMe(token);
  }, [fetchMe]);

  const login = useCallback(async (email: string, password: string) => {
    setState(s => ({ ...s, loading: true, error: null }));
    try {
      const res = await fetch(`${API}/api/auth/token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username: email, password }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Login failed" }));
        throw new Error(err.detail ?? "Login failed");
      }
      const { access_token } = await res.json();
      localStorage.setItem("cipher_token", access_token);

      const me = await fetch(`${API}/api/auth/me`, {
        headers: { Authorization: `Bearer ${access_token}` },
      });
      let resolvedEmail = email;
      let resolvedRole  = "user";
      if (me.ok) {
        const data = await me.json();
        resolvedEmail = data.email ?? email;
        resolvedRole  = data.role  ?? "user";
      }
      if (resolvedEmail) localStorage.setItem("cipher_email", resolvedEmail);
      if (resolvedRole)  localStorage.setItem("cipher_role",  resolvedRole);

      setState(s => ({
        ...s,
        token:           access_token,
        email:           resolvedEmail,
        role:            resolvedRole,
        isAuthenticated: true,
        isAdmin:         resolvedRole === "admin" || resolvedRole === "founder",
        loading:         false,
        error:           null,
      }));
    } catch (e: unknown) {
      setState(s => ({ ...s, loading: false, error: e instanceof Error ? e.message : "Login failed" }));
    }
  }, []);

  const register = useCallback(async (email: string, password: string) => {
    setState(s => ({ ...s, loading: true, error: null }));
    try {
      const res = await fetch(`${API}/api/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Registration failed" }));
        throw new Error(err.detail ?? "Registration failed");
      }
      setState(s => ({ ...s, loading: false }));
      await login(email, password);
    } catch (e: unknown) {
      setState(s => ({ ...s, loading: false, error: e instanceof Error ? e.message : "Registration failed" }));
    }
  }, [login]);

  const logout = useCallback(() => {
    clearStorage();
    setState({ ...INITIAL_STATE, ready: true });
  }, []);

  return { ...state, login, register, logout };
}
