"use client";
import { useState, useEffect, useCallback } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

interface AuthState {
  token:           string | null;
  email:           string | null;
  role:            string | null;   // 'user' | 'admin' | null
  isAuthenticated: boolean;
  isAdmin:         boolean;
  ready:           boolean;
}

export function useAuth(): AuthState & { logout: () => void } {
  const [state, setState] = useState<AuthState>({
    token:           null,
    email:           null,
    role:            null,
    isAuthenticated: false,
    isAdmin:         false,
    ready:           false,
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
      // Persist email so it survives page refresh without another /me call
      if (email) localStorage.setItem("cipher_email", email);
      if (role)  localStorage.setItem("cipher_role",  role);
      setState(s => ({ ...s, email, role, isAdmin: role === "admin" }));
    } catch { /* network error — keep existing state */ }
  }, []);

  useEffect(() => {
    const token = localStorage.getItem("cipher_token");
    const email = localStorage.getItem("cipher_email");
    const role  = localStorage.getItem("cipher_role");

    if (!token) {
      setState({ token: null, email: null, role: null,
                 isAuthenticated: false, isAdmin: false, ready: true });
      return;
    }

    // Optimistically set state from cache, then confirm with /me
    setState({
      token,
      email,
      role,
      isAuthenticated: true,
      isAdmin:         role === "admin",
      ready:           true,
    });

    // Refresh role from server (catches role changes without re-login)
    fetchMe(token);
  }, [fetchMe]);

  const logout = useCallback(() => {
    localStorage.removeItem("cipher_token");
    localStorage.removeItem("cipher_email");
    localStorage.removeItem("cipher_role");
    setState({ token: null, email: null, role: null,
               isAuthenticated: false, isAdmin: false, ready: true });
  }, []);

  return { ...state, logout };
}
