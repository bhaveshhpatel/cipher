"use client";
import { clsx } from "clsx";
import { DASHBOARD_TABS, TAB_META } from "@/types";
import type { DashboardTab } from "@/types";

export interface MobileTabBarProps {
  activeTab:   DashboardTab;
  onTabChange: (tab: DashboardTab) => void;
  signalCount: number;
}

export function MobileTabBar({ activeTab, onTabChange, signalCount }: MobileTabBarProps) {
  return (
    <nav
      data-testid="mobile-tab-bar"
      className="flex md:hidden items-center gap-1 px-4 overflow-x-auto"
      style={{
        background:   "var(--surface)",
        borderBottom: "1px solid var(--border)",
        paddingTop:   "0.5rem",
      }}
      role="navigation"
      aria-label="Dashboard navigation"
    >
      {DASHBOARD_TABS.map((tab) => {
        const { label, icon, shortLabel } = TAB_META[tab];
        const isActive  = tab === activeTab;
        const showBadge = tab === "signals" && signalCount > 0;

        return (
          <button
            key={tab}
            data-testid={`mobile-tab-${tab}`}
            onClick={() => onTabChange(tab)}
            className="relative flex items-center gap-1.5 px-3 py-2 text-sm font-medium whitespace-nowrap transition-all rounded-t-md shrink-0"
            style={{
              color:        isActive ? "var(--amber)" : "var(--muted)",
              background:   isActive ? "var(--surface-2)" : "transparent",
              borderBottom: isActive ? "2px solid var(--amber)" : "2px solid transparent",
            }}
            aria-current={isActive ? "page" : undefined}
          >
            <span>{icon}</span>
            <span className="hidden sm:inline">{label}</span>
            <span className="sm:hidden">{shortLabel}</span>
            {showBadge && (
              <span
                data-testid="mobile-signal-badge"
                className="ml-1 px-1.5 py-0.5 rounded-full text-xs font-bold font-mono"
                style={{ background: "var(--amber)", color: "#1a0f00", minWidth: 20, textAlign: "center" }}
              >
                {signalCount > 99 ? "99+" : signalCount}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
