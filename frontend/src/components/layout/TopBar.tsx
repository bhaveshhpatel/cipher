"use client";
import { CipherLogo }       from "@/components/CipherLogo";
import { ThemeToggle }      from "@/components/ThemeToggle";
import { MarketStatusChip } from "@/components/ui";
import { StreamStatsBar }   from "@/components/dashboard/StreamStatsBar";
import { useMarketStatus }  from "@/hooks";
import type { StreamStats } from "@/types";

export interface TopBarProps {
  email:    string | null;
  stats?:   StreamStats | null;
  onLogout: () => void;
}

export function TopBar({ email, stats, onLogout }: TopBarProps) {
  const { status, isLoading } = useMarketStatus();

  return (
    <header
      className="sticky top-0 z-40 flex items-center justify-between px-4 md:px-6 h-14"
      style={{ background: "var(--surface)", borderBottom: "1px solid var(--border)" }}
    >
      {/* Left: logo + wordmark + live market chip */}
      <div className="flex items-center gap-3">
        <CipherLogo size={28} />
        <span
          className="font-bold text-base tracking-tight"
          style={{ color: "var(--text)" }}
        >
          CIPHER
        </span>
        {!isLoading && status && (
          <span className="hidden sm:flex">
            <MarketStatusChip status={status} />
          </span>
        )}
      </div>

      {/* Right: stream stats + email + theme + sign-out */}
      <div className="flex items-center gap-3">
        {stats && <StreamStatsBar stats={stats} />}
        {email && (
          <span
            className="text-xs font-mono hidden sm:block"
            style={{ color: "var(--faint)" }}
          >
            {email}
          </span>
        )}
        <ThemeToggle />
        <button
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
