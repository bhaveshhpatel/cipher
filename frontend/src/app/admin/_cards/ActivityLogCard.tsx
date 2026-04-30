"use client";
import { useState } from "react";
import { useActivityLog } from "@/hooks/useActivityLog";
import { A, AdminCard, CardHeader } from "./_shared";

const KNOWN_ACTIONS = [
  "demo.start",
  "demo.stop",
  "ingestion_config.update",
  "tier_thresholds.update",
  "registry.prewarm",
];

const ACTION_COLOR: Record<string, { color: string; bg: string; border: string }> = {
  "demo.start":              { color: A.green,  bg: A.greenDim,  border: A.greenBorder  },
  "demo.stop":               { color: A.red,    bg: A.redDim,    border: A.redBorder    },
  "ingestion_config.update": { color: A.amber,  bg: A.amberDim,  border: A.amberBorder  },
  "tier_thresholds.update":  { color: A.indigo, bg: A.indigoDim, border: A.indigoBorder },
  "registry.prewarm":        { color: A.cyan,   bg: A.cyanDim,   border: A.cyanBorder   },
};

const PAGE_SIZE = 20;

function fmtDateTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString() + " " + d.toLocaleTimeString();
}

export function ActivityLogCard({ token }: { token: string | null }) {
  const [offset,       setOffset]       = useState(0);
  const [actionFilter, setActionFilter] = useState("");
  const [emailFilter,  setEmailFilter]  = useState("");
  const [since,        setSince]        = useState("");
  const [before,       setBefore]       = useState("");

  const { items, total, loading, error, refresh } = useActivityLog({
    token,
    limit:      PAGE_SIZE,
    offset,
    action:     actionFilter || null,
    adminEmail: emailFilter  || null,
    since:      since        || null,
    before:     before       || null,
  });

  const totalPages  = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const hasPrev     = offset > 0;
  const hasNext     = offset + PAGE_SIZE < total;

  const clearFilters = () => {
    setActionFilter("");
    setEmailFilter("");
    setSince("");
    setBefore("");
    setOffset(0);
  };

  const hasFilters = actionFilter || emailFilter || since || before;

  return (
    <AdminCard>
      <CardHeader
        title="Activity Log"
        subtitle="Audit trail of all mutating admin actions — newest first"
        action={
          <button
            onClick={() => refresh()}
            aria-label="Refresh activity log"
            className="text-xs font-mono px-2 py-1 rounded transition-colors"
            style={{ color: A.muted, border: `1px solid ${A.border}`, background: A.surface2 }}
          >
            ↻
          </button>
        }
      />

      {/* ── Filters ──────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select
          value={actionFilter}
          onChange={e => { setActionFilter(e.target.value); setOffset(0); }}
          aria-label="Filter by action"
          className="text-xs font-mono px-2 py-1.5 rounded"
          style={{
            background: A.bg,
            border: `1px solid ${A.border}`,
            color: actionFilter ? A.text : A.muted,
          }}
        >
          <option value="">All actions</option>
          {KNOWN_ACTIONS.map(a => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>

        <input
          type="text"
          placeholder="Filter by email"
          value={emailFilter}
          onChange={e => { setEmailFilter(e.target.value); setOffset(0); }}
          aria-label="Filter by admin email"
          className="text-xs font-mono px-2 py-1.5 rounded"
          style={{
            background: A.bg,
            border: `1px solid ${A.border}`,
            color: A.text,
            width: "160px",
          }}
        />

        <input
          type="datetime-local"
          value={since}
          onChange={e => { setSince(e.target.value); setOffset(0); }}
          aria-label="Since date"
          title="Since (lower bound, inclusive)"
          className="text-xs font-mono px-2 py-1.5 rounded"
          style={{
            background: A.bg,
            border: `1px solid ${since ? A.amberBorder : A.border}`,
            color: since ? A.text : A.muted,
          }}
        />

        <input
          type="datetime-local"
          value={before}
          onChange={e => { setBefore(e.target.value); setOffset(0); }}
          aria-label="Before date"
          title="Before (upper bound, inclusive)"
          className="text-xs font-mono px-2 py-1.5 rounded"
          style={{
            background: A.bg,
            border: `1px solid ${before ? A.amberBorder : A.border}`,
            color: before ? A.text : A.muted,
          }}
        />

        {hasFilters && (
          <button
            onClick={clearFilters}
            className="text-xs font-mono px-2 py-1.5 rounded"
            style={{ color: A.muted, border: `1px solid ${A.border}`, background: A.surface2 }}
          >
            ✕ Clear
          </button>
        )}

        {total > 0 && (
          <span className="ml-auto text-xs font-mono" style={{ color: A.faint }}>
            {total} total
          </span>
        )}
      </div>

      {/* ── States ───────────────────────────────────────── */}
      {loading && (
        <p className="text-xs font-mono py-2" style={{ color: A.muted }}>Loading…</p>
      )}

      {!loading && error && (
        <p
          className="text-xs font-mono p-3 rounded"
          style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
        >
          {error}
        </p>
      )}

      {!loading && !error && items.length === 0 && (
        <p className="text-xs font-mono py-6 text-center" style={{ color: A.muted }}>
          No log entries found.
        </p>
      )}

      {/* ── Table ────────────────────────────────────────── */}
      {!loading && items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs font-mono border-collapse">
            <thead>
              <tr style={{ borderBottom: `1px solid ${A.border}` }}>
                {["Time", "Admin", "Action", "Detail", "IP"].map(h => (
                  <th
                    key={h}
                    className="pb-2 pr-4 text-left font-semibold"
                    style={{ color: A.muted }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map(item => {
                const ac = ACTION_COLOR[item.action] ?? {
                  color: A.muted, bg: A.surface2, border: A.border,
                };
                const detailStr = Object.keys(item.detail).length > 0
                  ? JSON.stringify(item.detail)
                  : "—";
                return (
                  <tr
                    key={item.id}
                    style={{ borderBottom: `1px solid ${A.border}22` }}
                  >
                    <td
                      className="py-2 pr-4 whitespace-nowrap"
                      style={{ color: A.muted }}
                    >
                      {fmtDateTime(item.created_at)}
                    </td>
                    <td
                      className="py-2 pr-4 whitespace-nowrap"
                      style={{ color: A.text }}
                    >
                      {item.admin_email}
                    </td>
                    <td className="py-2 pr-4 whitespace-nowrap">
                      <span
                        className="px-2 py-0.5 rounded"
                        style={{
                          background: ac.bg,
                          color:      ac.color,
                          border:     `1px solid ${ac.border}`,
                        }}
                      >
                        {item.action}
                      </span>
                    </td>
                    <td
                      className="py-2 pr-4 max-w-xs truncate"
                      style={{ color: A.faint }}
                      title={detailStr !== "—" ? detailStr : undefined}
                    >
                      {detailStr}
                    </td>
                    <td className="py-2" style={{ color: A.faint }}>
                      {item.ip_address ?? "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Pagination ───────────────────────────────────── */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between mt-4 pt-4"
          style={{ borderTop: `1px solid ${A.border}` }}
        >
          <span className="text-xs font-mono" style={{ color: A.muted }}>
            Page {currentPage} of {totalPages} · {total} total
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={!hasPrev}
              aria-label="Previous page"
              className="text-xs font-mono px-3 py-1 rounded"
              style={{
                color:      hasPrev ? A.text : A.muted,
                border:     `1px solid ${A.border}`,
                background: A.surface2,
                cursor:     hasPrev ? "pointer" : "not-allowed",
              }}
            >
              ← Prev
            </button>
            <button
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={!hasNext}
              aria-label="Next page"
              className="text-xs font-mono px-3 py-1 rounded"
              style={{
                color:      hasNext ? A.text : A.muted,
                border:     `1px solid ${A.border}`,
                background: A.surface2,
                cursor:     hasNext ? "pointer" : "not-allowed",
              }}
            >
              Next →
            </button>
          </div>
        </div>
      )}
    </AdminCard>
  );
}
