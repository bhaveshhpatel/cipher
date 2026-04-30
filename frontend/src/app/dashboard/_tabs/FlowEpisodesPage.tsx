"use client";
import { useState } from "react";
import { useFlowEpisodes } from "@/hooks/useFlowEpisodes";
import type { FlowEpisodesFilters } from "@/hooks/useFlowEpisodes";
import { FlowEpisodesTab } from "@/components/dashboard/FlowEpisodesTab";

interface Props { token: string | null; }

export function FlowEpisodesPage({ token }: Props) {
  const [filters, setFilters] = useState<FlowEpisodesFilters>({});
  const { episodes, loading, error } = useFlowEpisodes(token, filters);

  return (
    <div className="flex flex-col gap-4" data-testid="flow-episodes-page">
      <div>
        <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Repetition Episodes</h1>
        <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
          Aggregated repetition clusters · 30s auto-refresh
        </p>
      </div>
      <FlowEpisodesTab
        episodes={episodes}
        loading={loading}
        error={error}
        onFiltersChange={setFilters}
      />
    </div>
  );
}
