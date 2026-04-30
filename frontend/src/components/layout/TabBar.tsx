"use client";
import { DASHBOARD_TABS, TAB_META } from "@/types";
import type { DashboardTab } from "@/types";

export interface TabBarProps {
  activeTab:    DashboardTab;
  onTabChange:  (tab: DashboardTab) => void;
  /** Live signal count — drives the badge on the Signals tab. */
  signalCount?: number;
}

export function TabBar({ activeTab, onTabChange, signalCount = 0 }: TabBarProps) {
  return (
    <nav
      className="flex items-center gap-1 px-4 md:px-6 overflow-x-auto"
      style={{
        background:   "var(--surface)",
        borderBottom: "1px solid var(--border)",
        paddingTop:   "0.5rem",
      }}
    >
      {DASHBOARD_TABS.map(id => {
        const meta     = TAB_META[id];
        const isActive = id === activeTab;

        return (
          <button
            key={id}
            onClick={() => onTabChange(id)}
            aria-current={isActive ? "page" : undefined}
            className="relative flex items-center gap-1.5 px-3 py-2 text-sm font-medium whitespace-nowrap transition-all rounded-t-md"
            style={{
              color:        isActive ? "var(--amber)" : "var(--muted)",
              background:   isActive ? "var(--surface-2)" : "transparent",
              borderBottom: isActive
                ? "2px solid var(--amber)"
                : "2px solid transparent",
            }}
          >
            <span aria-hidden="true">{meta.icon}</span>
            <span>{meta.label}</span>

            {id === "signals" && signalCount > 0 && (
              <span
                aria-label={`${signalCount > 99 ? "99+" : signalCount} live signals`}
                className="ml-1 px-1.5 py-0.5 rounded-full text-xs font-bold font-mono"
                style={{
                  background: "var(--amber)",
                  color:      "#1a0f00",
                  minWidth:   20,
                  textAlign:  "center",
                }}
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
