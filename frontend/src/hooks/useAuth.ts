"use client";
import { useState, useEffect } from "react";

export function useAuth() {
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [ready, setReady]  = useState(false);

  useEffect(() => {
    setToken(localStorage.getItem("cipher_token"));
    setEmail(localStorage.getItem("cipher_email"));
    setReady(true);
  }, []);

  const logout = () => {
    localStorage.removeItem("cipher_token");
    localStorage.removeItem("cipher_email");
    setToken(null);
    setEmail(null);
  };

  return { token, email, ready, logout };
}
