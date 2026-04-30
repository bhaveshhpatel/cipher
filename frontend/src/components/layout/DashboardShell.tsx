"use client";
import type { ReactNode } from "react";
import { TopBar } from "./TopBar";
import { TabBar } from "./TabBar";
import type { DashboardTab } from "@/types";
import type { StreamStats }  from "@/types";

export interface DashboardShellProps {
  /** Authenticated user email — displayed in the top bar. */
  email:        string | null;
  /** Currently active tab id. */
  tab:          DashboardTab;
  onTabChange:  (tab: DashboardTab) => void;
  onLogout:     () => void;
  /** Live signal count — drives the badge on the Signals tab. */
  signalCount?: number;
  /** Stream stats — rendered in the top bar when available. */
  stats?:       StreamStats | null;
  children:     ReactNode;
}

export function DashboardShell({
  email,
  tab,
  onTabChange,
  onLogout,
  signalCount = 0,
  stats,
  children,
}: DashboardShellProps) {
  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--bg)" }}>
      <TopBar email={email} stats={stats} onLogout={onLogout} />
      <TabBar
        activeTab={tab}
        onTabChange={onTabChange}
        signalCount={signalCount}
      />
      <main
        className="flex-1 p-4 md:p-6"
        style={{ maxWidth: 1400, width: "100%", margin: "0 auto" }}
      >
        {children}
      </main>
    </div>
  );
}
