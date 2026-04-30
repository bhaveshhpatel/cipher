"use client";
import { useEffect, useState, useCallback } from "react";
import { A, AdminCard, CardHeader } from "./_shared";

interface TierDistributionSample {
  symbol:        string;
  open_interest: number | null;
}

interface TierDistributionTier {
  count:   number;
  samples: TierDistributionSample[];
}

interface TierDistribution {
  snapshot_id: string;
  total:       number;
  tiers: {
    "1": TierDistributionTier;
    "2": TierDistributionTier;
    "3": TierDistributionTier;
  };
}

const TIER_COLORS = [
  { color: A.cyan,   dim: A.cyanDim,   border: A.cyanBorder   },
  { color: A.indigo, dim: A.indigoDim, border: A.indigoBorder },
  { color: A.amber,  dim: A.amberDim,  border: A.amberBorder  },
];

export function TierDistributionCard({ token }: { token: string | null }) {
  const [data,    setData]    = useState<TierDistribution | null>(null);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch("/api/admin/tier-distribution", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load tier distribution");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { load(); }, [load]);

  return (
    <AdminCard>
      <CardHeader
        title="Tier Distribution"
        subtitle="Symbol counts + OI samples per tier (from live registry snapshot)"
      />
      {loading && <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>}
      {err     && <p className="text-xs font-mono" style={{ color: A.red }}>{err}</p>}
      {data && !loading && (
        <>
          <p className="text-xs font-mono mb-4" style={{ color: A.muted }}>
            Snapshot{" "}
            <span style={{ color: A.faint }}>{data.snapshot_id}</span>
            {" · "}
            <span style={{ color: A.text }}>{data.total}</span> total symbols
          </p>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {(["1", "2", "3"] as const).map((tier, ti) => {
              const t = data.tiers[tier];
              const c = TIER_COLORS[ti];
              return (
                <div
                  key={tier}
                  className="rounded-lg p-4"
                  style={{ background: A.surface2, border: `1px solid ${A.border}` }}
                >
                  <div className="flex items-center justify-between mb-3">
                    <span
                      className="text-xs font-mono px-2 py-0.5 rounded"
                      style={{ background: c.dim, color: c.color, border: `1px solid ${c.border}` }}
                    >
                      TIER {tier}
                    </span>
                    <span className="text-sm font-semibold font-mono" style={{ color: A.text }}>
                      {t.count} symbols
                    </span>
                  </div>
                  <div className="space-y-1">
                    {t.samples.map(s => (
                      <div key={s.symbol} className="flex items-center justify-between">
                        <span className="text-xs font-mono" style={{ color: A.muted }}>{s.symbol}</span>
                        <span className="text-xs font-mono" style={{ color: A.faint }}>
                          OI: {s.open_interest?.toLocaleString() ?? "—"}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </AdminCard>
  );
}
