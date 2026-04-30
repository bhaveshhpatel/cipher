"use client";
import { clsx } from "clsx";
import { DASHBOARD_TABS, TAB_META } from "@/types";
import type { DashboardTab } from "@/types";

export interface SidebarNavProps {
  activeTab:   DashboardTab;
  onTabChange: (tab: DashboardTab) => void;
  signalCount: number;
  collapsed:   boolean;
  onToggle:    () => void;
}

export function SidebarNav({
  activeTab,
  onTabChange,
  signalCount,
  collapsed,
  onToggle,
}: SidebarNavProps) {
  return (
    <aside
      data-testid="sidebar-nav"
      className="hidden md:flex flex-col shrink-0 h-full transition-all duration-200"
      style={{
        width:       collapsed ? "var(--sidebar-width-collapsed)" : "var(--sidebar-width-expanded)",
        background:  "var(--surface)",
        borderRight: "1px solid var(--border)",
      }}
    >
      {/* Collapse toggle */}
      <button
        data-testid="sidebar-toggle"
        onClick={onToggle}
        className="flex items-center justify-center h-10 mt-2 mx-2 rounded-md transition-all text-sm"
        style={{ color: "var(--muted)", background: "var(--surface-2)" }}
        aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
      >
        {collapsed ? "\u203a" : "\u2039"}
      </button>

      <nav
        className="flex flex-col gap-1 p-2 mt-2"
        role="navigation"
        aria-label="Dashboard navigation"
      >
        {DASHBOARD_TABS.map((tab) => {
          const { label, icon } = TAB_META[tab];
          const isActive  = tab === activeTab;
          const showBadge = tab === "signals" && signalCount > 0;

          return (
            <button
              key={tab}
              data-testid={`sidebar-tab-${tab}`}
              onClick={() => onTabChange(tab)}
              className={clsx(
                "flex items-center gap-2 px-2 py-2.5 rounded-md text-sm font-medium transition-all w-full",
                collapsed ? "justify-center" : "justify-start",
              )}
              style={{
                color:      isActive ? "var(--amber)" : "var(--muted)",
                background: isActive ? "var(--surface-2)" : "transparent",
                borderLeft: isActive ? "2px solid var(--amber)" : "2px solid transparent",
              }}
              aria-current={isActive ? "page" : undefined}
            >
              <span className="shrink-0 text-base">{icon}</span>
              {!collapsed && (
                <span className="truncate flex-1">{label}</span>
              )}
              {showBadge && (
                <span
                  data-testid={`signal-badge-${tab}`}
                  className="ml-auto px-1.5 py-0.5 rounded-full text-xs font-bold font-mono shrink-0"
                  style={{ background: "var(--amber)", color: "#1a0f00", minWidth: 20, textAlign: "center" }}
                >
                  {signalCount > 99 ? "99+" : signalCount}
                </span>
              )}
            </button>
          );
        })}
      </nav>
    </aside>
  );
}
