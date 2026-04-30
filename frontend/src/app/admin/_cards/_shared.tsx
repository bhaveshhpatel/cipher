/**
 * _shared.tsx — Admin palette + shared sub-components
 * Imported by every _cards/*.tsx file.
 */
import React from "react";

/* ─── Admin palette ──────────────────────────────────────── */
export const A = {
  bg:           "#080c14",
  surface:      "#0e1422",
  surface2:     "#131927",
  border:       "#1e2d45",
  border2:      "#263754",
  text:         "#dce6f5",
  muted:        "#6b83a6",
  faint:        "#364d6b",
  cyan:         "#22d3ee",
  cyanDim:      "rgba(34,211,238,0.12)",
  cyanBorder:   "rgba(34,211,238,0.25)",
  indigo:       "#818cf8",
  indigoDim:    "rgba(129,140,248,0.12)",
  indigoBorder: "rgba(129,140,248,0.25)",
  amber:        "#fbbf24",
  amberDim:     "rgba(251,191,36,0.10)",
  amberBorder:  "rgba(251,191,36,0.25)",
  green:        "rgb(74,222,128)",
  greenDim:     "rgba(74,222,128,0.10)",
  greenBorder:  "rgba(74,222,128,0.25)",
  red:          "#f87171",
  redDim:       "rgba(248,113,113,0.10)",
  redBorder:    "rgba(248,113,113,0.25)",
} as const;

/* ─── AdminCard ──────────────────────────────────────────── */
export function AdminCard({
  children,
  className = "",
  style = {},
}: {
  children:   React.ReactNode;
  className?: string;
  style?:     React.CSSProperties;
}) {
  return (
    <div
      className={`rounded-xl p-6 ${className}`}
      style={{ background: A.surface, border: `1px solid ${A.border}`, ...style }}
    >
      {children}
    </div>
  );
}

/* ─── CardHeader ─────────────────────────────────────────── */
export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title:     string;
  subtitle?: string;
  action?:   React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between mb-5 gap-3">
      <div>
        <h2 className="text-base font-semibold font-mono tracking-tight" style={{ color: A.text }}>
          {title}
        </h2>
        {subtitle && (
          <p className="text-xs mt-1 font-mono leading-relaxed" style={{ color: A.muted }}>
            {subtitle}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}

/* ─── StatusPill ─────────────────────────────────────────── */
export function StatusPill({
  on,
  onLabel  = "● RUNNING",
  offLabel = "○ STOPPED",
  loading  = false,
}: {
  on:        boolean;
  onLabel?:  string;
  offLabel?: string;
  loading?:  boolean;
}) {
  return (
    <span
      className="text-xs font-mono px-3 py-1 rounded-full shrink-0"
      style={{
        border:     on ? `1px solid ${A.greenBorder}` : `1px solid ${A.border}`,
        background: on ? A.greenDim : A.surface2,
        color:      on ? A.green    : A.muted,
      }}
    >
      {loading ? "LOADING…" : on ? onLabel : offLabel}
    </span>
  );
}

/* ─── Stat ───────────────────────────────────────────────── */
export function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div
      className="rounded-lg p-3"
      style={{ background: A.surface2, border: `1px solid ${A.border}` }}
    >
      <p className="text-xs font-mono mb-1" style={{ color: A.muted }}>{label}</p>
      <p className="text-sm font-semibold font-mono tabular" style={{ color: A.text }}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
    </div>
  );
}

/* ─── FieldInput ─────────────────────────────────────────── */
export function FieldInput({
  value,
  onChange,
  onEnter,
  dirty,
  error,
}: {
  value:    string;
  onChange: (v: string) => void;
  onEnter:  () => void;
  dirty:    boolean;
  error:    string;
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={e => onChange(e.target.value)}
      onKeyDown={e => { if (e.key === "Enter") onEnter(); }}
      className="flex-1 px-3 py-1.5 rounded text-sm font-mono"
      style={{
        background: A.bg,
        border:     `1px solid ${error ? A.redBorder : dirty ? A.amberBorder : A.border}`,
        color:      A.text,
        outline:    "none",
      }}
    />
  );
}

/* ─── SaveBtn ────────────────────────────────────────────── */
export function SaveBtn({
  onClick,
  saving,
  saved,
  dirty,
}: {
  onClick: () => void;
  saving:  boolean;
  saved:   boolean;
  dirty:   boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={saving || !dirty}
      className="px-4 py-1.5 rounded text-xs font-mono font-semibold transition-colors"
      style={{
        minWidth:   "64px",
        background: saved  ? A.greenDim
                  : saving ? A.surface2
                  : !dirty ? A.surface2
                  : A.indigoDim,
        color:      saved  ? A.green
                  : !dirty ? A.muted
                  : A.indigo,
        border:     saved  ? `1px solid ${A.greenBorder}`
                  : !dirty ? `1px solid ${A.border}`
                  : `1px solid ${A.indigoBorder}`,
        cursor:     saving || !dirty ? "not-allowed" : "pointer",
      }}
    >
      {saved ? "✓ Saved" : saving ? "Saving…" : "Save"}
    </button>
  );
}

/* ─── Error banner ───────────────────────────────────────── */
export function ErrorBanner({ msg }: { msg: string }) {
  return (
    <p
      className="text-xs font-mono p-3 rounded mb-4"
      style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
    >
      {msg}
    </p>
  );
}
