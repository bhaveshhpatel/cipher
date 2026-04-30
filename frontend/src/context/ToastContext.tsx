"use client";
import toast, { Toaster } from "react-hot-toast";

// ─── Shared toast style ────────────────────────────────────────────────────

const base = {
  background:  "var(--surface)",
  color:       "var(--text)",
  border:      "1px solid var(--border)",
  borderRadius:"var(--radius-md)",
  fontSize:    "0.875rem",
  fontFamily:  "Inter, system-ui, sans-serif",
  boxShadow:   "var(--shadow-elevated)",
  padding:     "0.75rem 1rem",
};

// ─── useToast hook ────────────────────────────────────────────────────

export function useToast() {
  return {
    success: (msg: string) =>
      toast.success(msg, {
        style: { ...base, borderColor: "rgba(26,158,90,0.4)" },
        iconTheme: { primary: "var(--green)", secondary: "var(--surface)" },
        duration: 3000,
      }),

    error: (msg: string) =>
      toast.error(msg, {
        style: { ...base, borderColor: "rgba(220,53,69,0.4)" },
        iconTheme: { primary: "var(--red)", secondary: "var(--surface)" },
        duration: 5000,
      }),

    info: (msg: string) =>
      toast(msg, {
        style: base,
        icon: "•",
        duration: 3000,
      }),

    dismiss: toast.dismiss,
  };
}

// ─── ToastProvider ───────────────────────────────────────────────── */

export function ToastProvider({ children }: { children: React.ReactNode }) {
  return (
    <>
      {children}
      <Toaster
        position="bottom-right"
        gutter={8}
        toastOptions={{
          duration: 3000,
          style: base,
        }}
      />
    </>
  );
}
