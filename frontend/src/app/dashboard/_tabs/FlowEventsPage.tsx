"use client";
import { useState } from "react";
import { useFlowEvents } from "@/hooks/useFlowEvents";
import type { FlowEventsFilters } from "@/hooks/useFlowEvents";
import { FlowEventsTab } from "@/components/dashboard/FlowEventsTab";

interface Props { token: string | null; }

export function FlowEventsPage({ token }: Props) {
  const [filters, setFilters] = useState<FlowEventsFilters>({});
  const { events, loading, error } = useFlowEvents(token, filters);

  return (
    <div className="flex flex-col gap-4" data-testid="flow-events-page">
      <div>
        <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Flow Events</h1>
        <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
          Raw per-trade rows from live stream · 10s auto-refresh
        </p>
      </div>
      <FlowEventsTab
        events={events}
        loading={loading}
        error={error}
        onFiltersChange={setFilters}
      />
    </div>
  );
}
