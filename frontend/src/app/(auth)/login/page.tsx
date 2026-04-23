"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { authAPI } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [form,    setForm]    = useState({ username: "", password: "" });
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const data = await authAPI.login(form.username, form.password);
      localStorage.setItem("cipher_token", data.access_token);
      router.push("/dashboard");
    } catch (err: any) {
      setError(err.message ?? "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4"
         style={{ background: "var(--bg)" }}>
      <div className="w-full max-w-sm flex flex-col gap-6">

        {/* Logo */}
        <div className="flex flex-col items-center gap-2">
          <span className="text-4xl font-black font-mono tracking-tight"
                style={{ color: "var(--amber)" }}>⬡ CIPHER</span>
          <span className="text-xs uppercase tracking-widest"
                style={{ color: "var(--faint)" }}>Institutional Options Intelligence</span>
        </div>

        {/* Card */}
        <form onSubmit={handleSubmit}
              className="card p-6 flex flex-col gap-4">
          <h1 className="text-lg font-bold" style={{ color: "var(--text)" }}>Sign in</h1>

          {error && (
            <div className="rounded-md px-3 py-2 text-sm"
                 style={{ background: "rgba(220,53,69,0.1)", color: "var(--red)", border: "1px solid rgba(220,53,69,0.2)" }}>
              {error}
            </div>
          )}

          <div className="flex flex-col gap-1.5">
            <label className="text-xs font-semibold uppercase tracking-wider"
                   style={{ color: "var(--faint)" }}>Username</label>
            <input
              type="text" required autoComplete="username"
              value={form.username}
              onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
              className="rounded-md px-3 py-2.5 text-sm font-mono outline-none transition-all"
              style={{
                background:   "var(--surface-2)",
                border:       "1px solid var(--border)",
                color:        "var(--text)",
              }}
              onFocus={e => e.target.style.borderColor = "var(--amber)"}
              onBlur={e  => e.target.style.borderColor = "var(--border)"}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-xs font-semibold uppercase tracking-wider"
                   style={{ color: "var(--faint)" }}>Password</label>
            <input
              type="password" required autoComplete="current-password"
              value={form.password}
              onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
              className="rounded-md px-3 py-2.5 text-sm font-mono outline-none transition-all"
              style={{
                background: "var(--surface-2)",
                border:     "1px solid var(--border)",
                color:      "var(--text)",
              }}
              onFocus={e => e.target.style.borderColor = "var(--amber)"}
              onBlur={e  => e.target.style.borderColor = "var(--border)"}
            />
          </div>

          <button
            type="submit" disabled={loading}
            className="rounded-md py-2.5 text-sm font-bold uppercase tracking-wider transition-all"
            style={{
              background: loading ? "var(--border)" : "var(--amber)",
              color:      loading ? "var(--muted)"  : "#1a0f00",
            }}
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="text-center text-xs" style={{ color: "var(--faint)" }}>
          No account?{" "}
          <Link href="/register" style={{ color: "var(--amber)" }}>Create one</Link>
        </p>
      </div>
    </div>
  );
}
