"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useSignalStream } from "@/hooks/useSignalStream";
import { api } from "@/lib/api";
import type { StreamStats } from "@/lib/api";
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

export default function DashboardPage() {
  const router = useRouter();
  const { token, email, isAuthenticated, ready, logout } = useAuth();
  const { signals, connected } = useSignalStream(token);
  const [tab,   setTab]   = useState<DashboardTab>("flow_events");
  const [stats, setStats] = useState<StreamStats | null>(null);

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
      onTabChange={setTab}
      signalCount={signals.length}
    >
      {tab === "flow_events"   && <FlowEventsPage   token={token} />}
      {tab === "flow_episodes" && <FlowEpisodesPage token={token} />}
      {tab === "signals"       && <LiveSignalsPage  signals={signals} connected={connected} token={token} />}
      {tab === "simulation"    && <SimulationPage   token={token} />}
      {tab === "composite"     && <CompositePage    token={token} />}
      {tab === "history"       && <SignalHistoryPage token={token} />}
    </DashboardLayout>
  );
}
