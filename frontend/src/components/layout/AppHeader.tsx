"use client";
import { CipherLogo } from "@/components/CipherLogo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { StreamStatsBar } from "@/components/dashboard/StreamStatsBar";
import { MarketStatusChip } from "@/components/ui";
import { useMarketStatus } from "@/hooks";
import type { StreamStats } from "@/types";

export interface AppHeaderProps {
  email:    string | null;
  stats:    StreamStats | null;
  onLogout: () => void;
}

export function AppHeader({ email, stats, onLogout }: AppHeaderProps) {
  const { status, isLoading: statusLoading } = useMarketStatus();

  return (
    <header
      data-testid="app-header"
      className="sticky top-0 z-40 flex items-center justify-between px-4 md:px-6 h-14"
      style={{ background: "var(--surface)", borderBottom: "1px solid var(--border)" }}
    >
      <div className="flex items-center gap-3">
        <CipherLogo size={28} />
        <span
          className="font-bold text-base tracking-tight"
          style={{ color: "var(--text)" }}
        >
          CIPHER
        </span>
      </div>

      <div className="flex items-center gap-3">
        {!statusLoading && status && (
          <MarketStatusChip status={status} />
        )}
        {stats && <StreamStatsBar stats={stats} />}
        <span
          className="text-xs font-mono hidden sm:block"
          style={{ color: "var(--faint)" }}
        >
          {email}
        </span>
        <ThemeToggle />
        <button
          data-testid="logout-btn"
          onClick={onLogout}
          className="text-xs px-3 py-1.5 rounded-md transition-all font-mono"
          style={{
            background: "var(--surface-2)",
            border:     "1px solid var(--border)",
            color:      "var(--muted)",
          }}
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
