"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api as authAPI } from "@/lib/api";

export default function RegisterPage() {
  const router = useRouter();
  const [form,    setForm]    = useState({ email: "", password: "", confirm: "" });
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (form.password !== form.confirm) { setError("Passwords do not match"); return; }
    setLoading(true); setError(null);
    try {
      await authAPI.register(form.email, form.password);
      router.push("/login?registered=1");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Registration failed");
    } finally {
      setLoading(false);
    }
  }

  const inputStyle = {
    background: "var(--surface-2)",
    border:     "1px solid var(--border)",
    color:      "var(--text)",
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4"
         style={{ background: "var(--bg)" }}>
      <div className="w-full max-w-sm flex flex-col gap-6">
        <div className="flex flex-col items-center gap-2">
          <span className="text-4xl font-black font-mono tracking-tight"
                style={{ color: "var(--amber)" }}>⬡ CIPHER</span>
          <span className="text-xs uppercase tracking-widest"
                style={{ color: "var(--faint)" }}>Institutional Options Intelligence</span>
        </div>

        <form onSubmit={handleSubmit} className="card p-6 flex flex-col gap-4">
          <h1 className="text-lg font-bold" style={{ color: "var(--text)" }}>Create account</h1>

          {error && (
            <div className="rounded-md px-3 py-2 text-sm"
                 style={{ background: "rgba(220,53,69,0.1)", color: "var(--red)", border: "1px solid rgba(220,53,69,0.2)" }}>
              {error}
            </div>
          )}

          {[
            { label: "Email",    key: "email",    type: "email",    autoComplete: "email" },
            { label: "Password", key: "password", type: "password", autoComplete: "new-password" },
            { label: "Confirm Password", key: "confirm", type: "password", autoComplete: "new-password" },
          ].map(({ label, key, type, autoComplete }) => (
            <div key={key} className="flex flex-col gap-1.5">
              <label className="text-xs font-semibold uppercase tracking-wider"
                     style={{ color: "var(--faint)" }}>{label}</label>
              <input
                type={type} required autoComplete={autoComplete}
                value={(form as Record<string, string>)[key]}
                onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
                className="rounded-md px-3 py-2.5 text-sm font-mono outline-none transition-all"
                style={inputStyle}
                onFocus={e => e.target.style.borderColor = "var(--amber)"}
                onBlur={e  => e.target.style.borderColor = "var(--border)"}
              />
            </div>
          ))}

          <button type="submit" disabled={loading}
                  className="rounded-md py-2.5 text-sm font-bold uppercase tracking-wider transition-all"
                  style={{ background: loading ? "var(--border)" : "var(--amber)", color: loading ? "var(--muted)" : "#1a0f00" }}>
            {loading ? "Creating…" : "Create account"}
          </button>
        </form>

        <p className="text-center text-xs" style={{ color: "var(--faint)" }}>
          Already have an account?{" "}
          <Link href="/login" style={{ color: "var(--amber)" }}>Sign in</Link>
        </p>
      </div>
    </div>
  );
}
