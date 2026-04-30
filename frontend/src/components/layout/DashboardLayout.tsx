"use client";
import { useState } from "react";
import type { ReactNode } from "react";
import { AppHeader } from "./AppHeader";
import { SidebarNav } from "./SidebarNav";
import { MobileTabBar } from "./MobileTabBar";
import type { DashboardTab, StreamStats } from "@/types";

export interface DashboardLayoutProps {
  email:       string | null;
  stats:       StreamStats | null;
  onLogout:    () => void;
  activeTab:   DashboardTab;
  onTabChange: (tab: DashboardTab) => void;
  signalCount: number;
  children:    ReactNode;
}

export function DashboardLayout({
  email,
  stats,
  onLogout,
  activeTab,
  onTabChange,
  signalCount,
  children,
}: DashboardLayoutProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  return (
    <div
      data-testid="dashboard-layout"
      className="min-h-screen flex flex-col"
      style={{ background: "var(--bg)" }}
    >
      <AppHeader email={email} stats={stats} onLogout={onLogout} />

      {/* Mobile-only horizontal tab bar */}
      <MobileTabBar
        activeTab={activeTab}
        onTabChange={onTabChange}
        signalCount={signalCount}
      />

      {/* Body row: sidebar (desktop) + scrollable content */}
      <div className="flex flex-1 min-h-0">
        <SidebarNav
          activeTab={activeTab}
          onTabChange={onTabChange}
          signalCount={signalCount}
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(c => !c)}
        />

        <main
          data-testid="layout-main"
          className="flex-1 p-4 md:p-6 overflow-auto"
          style={{ maxWidth: 1400, width: "100%", margin: "0 auto" }}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
