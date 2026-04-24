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

export function useAuth() {
  const [state, setState] = useState<AuthState>({
    token:           null,
    email:           null,
    role:            null,
    isAuthenticated: false,
    isAdmin:         false,
    ready:           false,
    loading:         false,
    error:           null,
  });

  const fetchMe = useCallback(async (token: string) => {
    try {
      const res = await fetch(`${API}/api/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const data = await res.json();
      const email = data.email ?? null;
      const role  = data.role  ?? "user";
      if (email) localStorage.setItem("cipher_email", email);
      if (role)  localStorage.setItem("cipher_role",  role);
      setState(s => ({ ...s, email, role, isAdmin: role === "admin" }));
    } catch { /* non-fatal */ }
  }, []);

  // Initialise from localStorage on mount
  useEffect(() => {
    const token = localStorage.getItem("cipher_token");
    const email = localStorage.getItem("cipher_email");
    const role  = localStorage.getItem("cipher_role");

    if (!token) {
      setState(s => ({ ...s, token: null, email: null, role: null,
                       isAuthenticated: false, isAdmin: false, ready: true }));
      return;
    }

    // Optimistic state from cache
    setState(s => ({
      ...s,
      token,
      email,
      role,
      isAuthenticated: true,
      isAdmin:         role === "admin",
      ready:           true,
    }));

    // Confirm + refresh role from server
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

      // Fetch email + role from /me
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
        isAdmin:         resolvedRole === "admin",
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
      // Auto-login after register
      setState(s => ({ ...s, loading: false }));
      await login(email, password);
    } catch (e: unknown) {
      setState(s => ({ ...s, loading: false, error: e instanceof Error ? e.message : "Registration failed" }));
    }
  }, [login]);

  const logout = useCallback(() => {
    localStorage.removeItem("cipher_token");
    localStorage.removeItem("cipher_email");
    localStorage.removeItem("cipher_role");
    setState({ token: null, email: null, role: null,
               isAuthenticated: false, isAdmin: false,
               ready: true, loading: false, error: null });
  }, []);

  return { ...state, login, register, logout };
}
