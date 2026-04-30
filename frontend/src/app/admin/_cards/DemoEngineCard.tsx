"use client";
import { type DemoStatus } from "@/hooks/useAdminDemo";
import { A, AdminCard, CardHeader, ErrorBanner, Stat, StatusPill } from "./_shared";

// DemoStatus is the canonical type from useAdminDemo:
// { demo: DemoStats; admin: string; role: string }
// The card only reads demo.*, so the extra admin/role fields are ignored.

interface Props {
  status:    DemoStatus | null;
  isRunning: boolean;
  loading:   boolean;
  error:     string | null;
  toggle:    (on: boolean) => void;
}

export function DemoEngineCard({ status, isRunning, loading, error, toggle }: Props) {
  return (
    <AdminCard>
      <CardHeader
        title="Demo Engine"
        subtitle="Simulated flow for UI testing when markets are closed"
        action={<StatusPill on={isRunning} loading={loading} />}
      />

      {error && <ErrorBanner msg={error} />}

      {status && (
        <div className="grid grid-cols-2 gap-3 mb-5">
          <Stat label="Ticks Emitted"     value={status.demo.ticks_emitted} />
          <Stat label="Signals Generated" value={status.demo.signals_generated} />
          <Stat label="Last Ticker"       value={status.demo.last_ticker ?? "—"} />
          <Stat
            label="Started At"
            value={
              status.demo.started_at
                ? new Date(status.demo.started_at).toLocaleTimeString()
                : "—"
            }
          />
        </div>
      )}

      <div className="flex gap-3">
        <button
          onClick={() => toggle(true)}
          disabled={isRunning || loading}
          data-testid="btn-start"
          className="flex-1 py-2 rounded text-xs font-mono font-semibold transition-colors"
          style={{
            background: isRunning ? A.surface2 : A.greenDim,
            color:      isRunning ? A.muted    : A.green,
            border:     isRunning ? `1px solid ${A.border}` : `1px solid ${A.greenBorder}`,
            cursor:     isRunning || loading ? "not-allowed" : "pointer",
          }}
        >
          ▶ Start
        </button>
        <button
          onClick={() => toggle(false)}
          disabled={!isRunning || loading}
          data-testid="btn-stop"
          className="flex-1 py-2 rounded text-xs font-mono font-semibold transition-colors"
          style={{
            background: !isRunning ? A.surface2 : A.redDim,
            color:      !isRunning ? A.muted    : A.red,
            border:     !isRunning ? `1px solid ${A.border}` : `1px solid ${A.redBorder}`,
            cursor:     !isRunning || loading ? "not-allowed" : "pointer",
          }}
        >
          ■ Stop
        </button>
      </div>
    </AdminCard>
  );
}
