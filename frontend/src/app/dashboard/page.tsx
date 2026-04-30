"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useSignalStream } from "@/hooks/useSignalStream";
import { api } from "@/lib/api";
import type { StreamStats } from "@/lib/api";
import { DASHBOARD_TABS } from "@/types";
import type { DashboardTab } from "@/types";

import { DashboardLayout } from "@/components/layout";
import {
  FlowEventsPage,
  FlowEpisodesPage,
  LiveSignalsPage,
  SimulationPage,
  CompositePage,
  SignalHistoryPage,
} from "./_tabs";

const STATS_REFRESH_MS = 15_000;

// Tab content lookup — avoids per-tab conditional chains.
// Props that change over time (signals, connected) are passed inline so React
// re-renders the mounted-but-hidden component with fresh values.
function TabContent({
  t, token, signals, connected,
}: {
  t:         DashboardTab;
  token:     string | null;
  signals:   ReturnType<typeof useSignalStream>["signals"];
  connected: boolean;
}) {
  if (t === "flow_events")   return <FlowEventsPage   token={token} />;
  if (t === "flow_episodes") return <FlowEpisodesPage token={token} />;
  if (t === "signals")       return <LiveSignalsPage  signals={signals} connected={connected} token={token} />;
  if (t === "simulation")    return <SimulationPage   token={token} />;
  if (t === "composite")     return <CompositePage    token={token} />;
  if (t === "history")       return <SignalHistoryPage token={token} />;
  return null;
}

export default function DashboardPage() {
  const router = useRouter();
  const { token, email, isAuthenticated, ready, logout } = useAuth();
  const { signals, connected } = useSignalStream(token);
  const [tab,     setTab]     = useState<DashboardTab>("flow_events");
  const [stats,   setStats]   = useState<StreamStats | null>(null);
  // Tracks which tabs have mounted at least once.
  // Once visited a tab stays in the DOM (hidden via CSS) so its local state
  // — filters, ticker, sim results, countdown — survives tab switches.
  const [visited, setVisited] = useState<Set<DashboardTab>>(new Set(["flow_events"]));

  const handleTabChange = (t: DashboardTab) => {
    setTab(t);
    setVisited(prev => new Set([...prev, t]));
  };

  // Auth guard
  useEffect(() => {
    if (!ready) return;
    if (!isAuthenticated) router.replace("/");
  }, [ready, isAuthenticated, router]);

  // Stream stats polling
  useEffect(() => {
    if (!token) return;
    const load = async () => {
      try { const d = await api.getStats(token); setStats(d.stats); } catch {}
    };
    load();
    const iv = setInterval(load, STATS_REFRESH_MS);
    return () => clearInterval(iv);
  }, [token]);

  if (!ready || !isAuthenticated) return null;

  return (
    <DashboardLayout
      email={email}
      stats={stats}
      onLogout={logout}
      activeTab={tab}
      onTabChange={handleTabChange}
      signalCount={signals.length}
    >
      {DASHBOARD_TABS.map(t =>
        visited.has(t) && (
          <div key={t} style={{ display: tab === t ? undefined : "none" }}>
            <TabContent t={t} token={token} signals={signals} connected={connected} />
          </div>
        )
      )}
    </DashboardLayout>
  );
}
