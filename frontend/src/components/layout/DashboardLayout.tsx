"use client";
import { useState } from "react";
import type { ReactNode } from "react";
import { AppHeader }    from "./AppHeader";
import { SidebarNav }   from "./SidebarNav";
import { MobileTabBar } from "./MobileTabBar";
import type { DashboardTab, StreamStats } from "@/types";

const SIDEBAR_KEY = "cipher:sidebar-collapsed";

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
  // ── sidebar collapse — persisted to localStorage ──────────────────────────
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(SIDEBAR_KEY) === "true";
  });

  const handleToggle = () => {
    setSidebarCollapsed(prev => {
      const next = !prev;
      localStorage.setItem(SIDEBAR_KEY, String(next));
      return next;
    });
  };

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
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <SidebarNav
          activeTab={activeTab}
          onTabChange={onTabChange}
          signalCount={signalCount}
          collapsed={sidebarCollapsed}
          onToggle={handleToggle}
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
