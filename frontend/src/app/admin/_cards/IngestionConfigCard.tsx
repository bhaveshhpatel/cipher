"use client";
import { useEffect, useState, useCallback } from "react";
import { A, AdminCard, CardHeader, FieldInput, SaveBtn } from "./_shared";

interface ConfigRow {
  key:         string;
  value:       string;
  value_type:  string;
  description: string;
  updated_at:  string;
  updated_by:  string | null;
}

export function IngestionConfigCard({ token }: { token: string | null }) {
  const [rows,    setRows]    = useState<ConfigRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState<string | null>(null);
  const [drafts,  setDrafts]  = useState<Record<string, string>>({});
  const [saving,  setSaving]  = useState<Record<string, boolean>>({});
  const [saved,   setSaved]   = useState<Record<string, boolean>>({});
  const [errors,  setErrors]  = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch("/api/admin/ingestion/config", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRows(Array.isArray(data) ? data : (data.config ?? []));
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load config");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const save = useCallback(async (key: string) => {
    if (!token) return;
    const value = drafts[key];
    setSaving(p => ({ ...p, [key]: true }));
    try {
      const res = await fetch("/api/admin/ingestion/config", {
        method:  "PATCH",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body:    JSON.stringify({ key, value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
      setDrafts(p => { const n = { ...p }; delete n[key]; return n; });
      setSaved(p => ({ ...p, [key]: true }));
      setTimeout(() => setSaved(p => ({ ...p, [key]: false })), 2000);
    } catch (e: unknown) {
      setErrors(p => ({ ...p, [key]: e instanceof Error ? e.message : "Save failed" }));
    } finally {
      setSaving(p => ({ ...p, [key]: false }));
    }
  }, [token, drafts, load]);

  return (
    <AdminCard>
      <CardHeader title="Ingestion Config" subtitle="Runtime config values stored in DB" />
      {loading && <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>}
      {err     && <p className="text-xs font-mono" style={{ color: A.red }}>{err}</p>}
      {!loading && rows.length === 0 && !err && (
        <p className="text-xs font-mono" style={{ color: A.muted }}>No config rows found.</p>
      )}
      {rows.map(row => {
        const draft  = drafts[row.key] ?? row.value;
        const dirty  = draft !== row.value;
        const errMsg = errors[row.key] ?? "";
        return (
          <div key={row.key} data-testid={`row-${row.key}`} className="mb-3">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono w-48 shrink-0" style={{ color: A.muted }}>
                {row.key}
              </span>
              <FieldInput
                value={draft}
                onChange={v => {
                  setDrafts(p => ({ ...p, [row.key]: v }));
                  setErrors(p => { const n = { ...p }; delete n[row.key]; return n; });
                }}
                onEnter={() => save(row.key)}
                dirty={dirty}
                error={errMsg}
              />
              <SaveBtn
                onClick={() => save(row.key)}
                saving={!!saving[row.key]}
                saved={!!saved[row.key]}
                dirty={dirty}
              />
            </div>
            {row.description && (
              <p className="text-xs font-mono mt-1 ml-52" style={{ color: A.faint }}>
                {row.description}
              </p>
            )}
            {errMsg && (
              <p className="text-xs font-mono mt-1 ml-52" style={{ color: A.red }}>{errMsg}</p>
            )}
          </div>
        );
      })}
    </AdminCard>
  );
}
