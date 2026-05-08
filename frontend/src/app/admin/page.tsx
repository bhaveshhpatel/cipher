"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useAdminDemo } from "@/hooks/useAdminDemo";
import { A } from "./_cards/_shared";
import { DemoEngineCard } from "./_cards/DemoEngineCard";
import { StreamHealthCard } from "./_cards/StreamHealthCard";
import { TierThresholdsCard } from "./_cards/TierThresholdsCard";
import { IngestionConfigCard } from "./_cards/IngestionConfigCard";
import { HowItWorksCard } from "./_cards/HowItWorksCard";
import { TierDistributionCard } from "./_cards/TierDistributionCard";
import { ActivityLogCard } from "./_cards/ActivityLogCard";
import { GateControlPanel } from "./_cards/GateControlPanel";

export default function AdminPage() {
  const router = useRouter();
  const { token, email, isAdmin, isAuthenticated, ready } = useAuth();
  const { status, isRunning, loading, error, toggle } = useAdminDemo(token);

  useEffect(() => {
    if (!ready) return;
    if (!isAuthenticated) { router.replace("/");          return; }
    if (!isAdmin)         { router.replace("/dashboard");  return; }
  }, [ready, isAuthenticated, isAdmin, router]);

  if (!ready || !isAdmin) return null;

  return (
    <div className="min-h-screen" style={{ background: A.bg, color: A.text }}>

      {/* ── Top bar ──────────────────────────────────── */}
      <div
        className="sticky top-0 z-10 flex items-center justify-between px-8 py-4"
        style={{
          background:     `${A.surface}cc`,
          borderBottom:   `1px solid ${A.border}`,
          backdropFilter: "blur(12px)",
        }}
      >
        <div className="flex items-center gap-3">
          <span
            className="text-xs font-mono px-2 py-0.5 rounded"
            style={{ background: A.cyanDim, color: A.cyan, border: `1px solid ${A.cyanBorder}` }}
          >
            ADMIN
          </span>
          <h1 className="text-sm font-semibold font-mono tracking-wide" style={{ color: A.text }}>
            Cipher Control Panel
          </h1>
        </div>
        <div className="flex items-center gap-4">
          <span className="text-xs font-mono" style={{ color: A.muted }}>{email}</span>
          <button
            onClick={() => router.push("/dashboard")}
            className="text-xs font-mono transition-colors px-3 py-1.5 rounded"
            style={{ color: A.muted, border: `1px solid ${A.border}`, background: A.surface2 }}
          >
            ← Dashboard
          </button>
        </div>
      </div>

      {/* ── Body ──────────────────────────────────────── */}
      <div className="p-8 space-y-6">

        {/* Row 1: Demo Engine + Stream Health */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <DemoEngineCard
            status={status}
            isRunning={isRunning}
            loading={loading}
            error={error}
            toggle={toggle}
          />
          <StreamHealthCard token={token} />
        </div>

        {/* Row 2: Tier Thresholds + Ingestion Config */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <TierThresholdsCard token={token} />
          <IngestionConfigCard token={token} />
        </div>

        {/* Row 3: Gate Control Panel — ADMIN-UI-001 (ING-010 frontend surface) */}
        <GateControlPanel token={token} isAdmin={isAdmin} />

        {/* Row 4: Pipeline Overview */}
        <HowItWorksCard />

        {/* Row 5: Tier Distribution */}
        <TierDistributionCard token={token} />

        {/* Row 6: Activity Log — STORY-BE-001 */}
        <ActivityLogCard token={token} />

      </div>
    </div>
  );
}
