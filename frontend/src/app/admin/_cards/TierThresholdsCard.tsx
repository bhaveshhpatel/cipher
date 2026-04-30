"use client";
import { useEffect, useState, useCallback } from "react";
import { A, AdminCard, CardHeader, FieldInput, SaveBtn } from "./_shared";

interface TierThresholdsRow {
  id:                 number;
  updated_at:         string;
  updated_by:         string | null;
  is_active:          boolean;
  t1_min_volume:      number;
  t1_min_last_price:  number;
  t1_min_oi:          number;
  t1_atm_pct:         number;
  t1_max_dte:         number;
  t2_min_volume:      number;
  t2_min_last_price:  number;
  t2_min_oi:          number;
  t2_atm_pct:         number;
  t2_max_dte:         number;
  t3_min_volume:      number;
  t3_min_last_price:  number;
  t3_min_oi:          number;
  t3_atm_pct:         number;
  t3_max_dte:         number;
}

interface CacheMeta {
  warm:        boolean;
  age_seconds: number | null;
  ttl_seconds: number;
}

const TIER_FIELDS: { label: string; field: keyof TierThresholdsRow }[][] = [
  [
    { label: "T1 Min Volume",     field: "t1_min_volume"     },
    { label: "T1 Min Last Price", field: "t1_min_last_price" },
    { label: "T1 Min OI",         field: "t1_min_oi"         },
    { label: "T1 ATM %",          field: "t1_atm_pct"        },
    { label: "T1 Max DTE",        field: "t1_max_dte"        },
  ],
  [
    { label: "T2 Min Volume",     field: "t2_min_volume"     },
    { label: "T2 Min Last Price", field: "t2_min_last_price" },
    { label: "T2 Min OI",         field: "t2_min_oi"         },
    { label: "T2 ATM %",          field: "t2_atm_pct"        },
    { label: "T2 Max DTE",        field: "t2_max_dte"        },
  ],
  [
    { label: "T3 Min Volume",     field: "t3_min_volume"     },
    { label: "T3 Min Last Price", field: "t3_min_last_price" },
    { label: "T3 Min OI",         field: "t3_min_oi"         },
    { label: "T3 ATM %",          field: "t3_atm_pct"        },
    { label: "T3 Max DTE",        field: "t3_max_dte"        },
  ],
];

export function TierThresholdsCard({ token }: { token: string | null }) {
  const [data,    setData]    = useState<{ row: TierThresholdsRow; cache: CacheMeta } | null>(null);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState<string | null>(null);
  const [drafts,  setDrafts]  = useState<Record<string, string>>({});
  const [saving,  setSaving]  = useState<Record<string, boolean>>({});
  const [saved,   setSaved]   = useState<Record<string, boolean>>({});
  const [errors,  setErrors]  = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch("/api/admin/tier-thresholds", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load tier thresholds");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const save = useCallback(async (field: string) => {
    if (!token || !data) return;
    const raw = drafts[field];
    const num = Number(raw);
    if (isNaN(num)) {
      setErrors(p => ({ ...p, [field]: "Must be a number" }));
      return;
    }
    setSaving(p => ({ ...p, [field]: true }));
    try {
      const res = await fetch("/api/admin/tier-thresholds", {
        method:  "PATCH",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body:    JSON.stringify({ updates: { [field]: num } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated = await res.json();
      setData(prev => prev ? { ...prev, row: updated.row } : prev);
      setDrafts(p => { const n = { ...p }; delete n[field]; return n; });
      setSaved(p => ({ ...p, [field]: true }));
      setTimeout(() => setSaved(p => ({ ...p, [field]: false })), 2000);
    } catch (e: unknown) {
      setErrors(p => ({ ...p, [field]: e instanceof Error ? e.message : "Save failed" }));
    } finally {
      setSaving(p => ({ ...p, [field]: false }));
    }
  }, [token, data, drafts]);

  const row = data?.row ?? null;

  return (
    <AdminCard>
      <CardHeader
        title="Tier Thresholds"
        subtitle="Screening parameters for T1 / T2 / T3 classification"
      />
      {loading && <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>}
      {err     && <p className="text-xs font-mono" style={{ color: A.red }}>{err}</p>}
      {row && !loading && (
        <div className="space-y-4">
          {TIER_FIELDS.map((tierFields, ti) => (
            <div key={ti}>
              <p className="text-xs font-mono mb-2" style={{ color: A.cyan }}>Tier {ti + 1}</p>
              <div className="space-y-2">
                {tierFields.map(({ label, field }) => {
                  const current = String(row[field]);
                  const draft   = drafts[field] ?? current;
                  const dirty   = draft !== current;
                  const errMsg  = errors[field] ?? "";
                  return (
                    <div key={field} data-testid={`field-${field as string}`} className="flex items-center gap-2">
                      <span className="text-xs font-mono w-36 shrink-0" style={{ color: A.muted }}>
                        {label}
                      </span>
                      <FieldInput
                        value={draft}
                        onChange={v => {
                          setDrafts(p => ({ ...p, [field]: v }));
                          setErrors(p => { const n = { ...p }; delete n[field]; return n; });
                        }}
                        onEnter={() => save(field as string)}
                        dirty={dirty}
                        error={errMsg}
                      />
                      <SaveBtn
                        onClick={() => save(field as string)}
                        saving={!!saving[field]}
                        saved={!!saved[field]}
                        dirty={dirty}
                      />
                      {errMsg && (
                        <span className="text-xs font-mono" style={{ color: A.red }}>{errMsg}</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </AdminCard>
  );
}
