"use client";
import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";

interface AuthState {
  token: string | null;
  email: string | null;
  isAuthenticated: boolean;
  loading: boolean;
  error: string | null;
}

// Module-level memory (survives re-renders, lost on hard refresh — by design)
let memToken: string | null = null;
let memEmail: string | null = null;

export function useAuth() {
  const [s, set] = useState<AuthState>({
    token: memToken,
    email: memEmail,
    isAuthenticated: !!memToken,
    loading: false,
    error: null,
  });

  useEffect(() => {
    set(x => ({ ...x, token: memToken, email: memEmail, isAuthenticated: !!memToken }));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    set(x => ({ ...x, loading: true, error: null }));
    try {
      const d = await api.login(email, password);
      memToken = d.access_token;
      memEmail = email;
      set({ token: d.access_token, email, isAuthenticated: true, loading: false, error: null });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Login failed";
      set(x => ({ ...x, loading: false, error: msg }));
    }
  }, []);

  const register = useCallback(async (email: string, password: string) => {
    set(x => ({ ...x, loading: true, error: null }));
    try {
      // Step 1: create account
      await api.register(email, password);
      // Step 2: only auto-login if registration succeeded
      await login(email, password);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Registration failed";
      // Don't overwrite a login error that may have been set by login()
      set(x => ({ ...x, loading: false, error: x.error ?? msg }));
    }
  }, [login]);

  const logout = useCallback(() => {
    memToken = null;
    memEmail = null;
    set({ token: null, email: null, isAuthenticated: false, loading: false, error: null });
    window.location.href = "/";
  }, []);

  return { ...s, login, register, logout };
}
