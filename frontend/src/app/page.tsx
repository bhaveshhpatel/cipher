"use client";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { CipherLogo } from "@/components/CipherLogo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { useAuth } from "@/hooks/useAuth";

export default function LoginPage() {
  const router = useRouter();
  const { login, register, isAuthenticated, loading, error } = useAuth();
  const [mode,     setMode]     = useState<"login" | "register">("login");
  const [email,    setEmail]    = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => {
    if (isAuthenticated) router.push("/dashboard");
  }, [isAuthenticated, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === "login") await login(email, password);
    else                  await register(email, password);
  };

  return (
    <div className="min-h-dvh flex flex-col items-center justify-center p-6 relative"
         style={{ background: "var(--bg)" }}>

      {/* Theme toggle — top right */}
      <div className="fixed top-4 right-4 z-50">
        <ThemeToggle />
      </div>

      {/* Subtle radial glow behind card (light: amber, dark: blue) */}
      <div
        className="fixed pointer-events-none"
        style={{
          top: "40%", left: "50%", transform: "translate(-50%, -50%)",
          width: 700, height: 700,
          background: "radial-gradient(circle, rgba(232,160,32,0.07) 0%, transparent 65%)",
        }}
      />

      {/* Logo + tagline */}
      <div className="mb-8 flex flex-col items-center gap-2">
        <CipherLogo size={72} showTagline />
      </div>

      {/* Card */}
      <div
        className="w-full max-w-[420px] rounded-xl p-8 flex flex-col gap-6 animate-fade-up"
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          boxShadow: "0 4px 24px rgba(0,0,0,0.07), 0 1px 4px rgba(0,0,0,0.05)",
        }}
      >
        {/* Amber accent top rule */}
        <div
          className="absolute top-0 left-[20%] right-[20%] h-px"
          style={{ background: "linear-gradient(90deg, transparent, var(--amber), transparent)" }}
        />

        {/* Mode tabs */}
        <div
          className="flex rounded-lg overflow-hidden"
          style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}
        >
          {(["login", "register"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className="flex-1 py-2.5 text-xs font-bold uppercase tracking-widest transition-all"
              style={{
                background:   mode === m ? "var(--amber)" : "transparent",
                color:        mode === m ? "#1a0f00"      : "var(--faint)",
                borderRight:  m === "login" ? "1px solid var(--border)" : "none",
              }}
            >
              {m === "login" ? "Sign In" : "Register"}
            </button>
          ))}
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {/* Email */}
          <div className="flex flex-col gap-1.5">
            <label
              className="text-xs font-semibold uppercase tracking-widest"
              style={{ color: "var(--muted)" }}
            >
              Email
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              placeholder="trader@cipher.io"
              className="w-full px-3.5 py-3 rounded-lg text-sm font-mono outline-none transition-all"
              style={{
                background:   "var(--surface-2)",
                border:       "1px solid var(--border)",
                color:        "var(--text)",
              }}
              onFocus={(e) => (e.target.style.borderColor = "var(--amber)")}
              onBlur={(e)  => (e.target.style.borderColor = "var(--border)")}
            />
          </div>

          {/* Password */}
          <div className="flex flex-col gap-1.5">
            <label
              className="text-xs font-semibold uppercase tracking-widest"
              style={{ color: "var(--muted)" }}
            >
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              placeholder="••••••••"
              className="w-full px-3.5 py-3 rounded-lg text-sm font-mono outline-none transition-all"
              style={{
                background:   "var(--surface-2)",
                border:       "1px solid var(--border)",
                color:        "var(--text)",
              }}
              onFocus={(e) => (e.target.style.borderColor = "var(--amber)")}
              onBlur={(e)  => (e.target.style.borderColor = "var(--border)")}
            />
          </div>

          {/* Error */}
          {error && (
            <div
              className="px-3.5 py-2.5 rounded-lg text-sm font-mono"
              style={{
                background:   "rgba(220,53,69,0.07)",
                border:       "1px solid rgba(220,53,69,0.25)",
                color:        "var(--red)",
              }}
            >
              ⚠ {error}
            </div>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={loading}
            className="btn btn-primary w-full py-3 text-sm mt-1"
          >
            {loading ? (
              <span className="flex items-center gap-2">
                <svg className="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
                </svg>
                {mode === "login" ? "Signing in…" : "Creating account…"}
              </span>
            ) : (
              mode === "login" ? "Sign In" : "Create Account"
            )}
          </button>
        </form>

        {/* Footer link */}
        <p className="text-center text-xs" style={{ color: "var(--faint)" }}>
          {mode === "login" ? "Don't have an account? " : "Already have an account? "}
          <button
            onClick={() => setMode(mode === "login" ? "register" : "login")}
            className="font-semibold transition-colors hover:opacity-80"
            style={{ color: "var(--amber)" }}
          >
            {mode === "login" ? "Register" : "Sign In"}
          </button>
        </p>
      </div>

      {/* Footer tagline */}
      <p className="mt-8 text-xs text-center" style={{ color: "var(--faint)" }}>
        Institutional options flow intelligence · Real-time whale detection
      </p>
    </div>
  );
}
