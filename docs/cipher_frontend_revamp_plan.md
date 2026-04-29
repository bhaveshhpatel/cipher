# Cipher Frontend Revamp — Master Plan & Technical Stories
> **Generated:** 2026-04-29 | **Author:** Principal UX Designer (PUD) × Principal Frontend Engineer (PFE)
> **Scope:** `/dashboard` and `/admin` pages — full revamp to production-grade, professional, responsive, high-performance UI

---

## Table of Contents
1. [Product & Audience Context](#1-product--audience-context)
2. [Current State Audit](#2-current-state-audit)
3. [PUD Design Vision](#3-pud-design-vision)
4. [PFE Architecture Plan](#4-pfe-architecture-plan)
5. [Design System Tokens](#5-design-system-tokens)
6. [Component Decomposition Map](#6-component-decomposition-map)
7. [Master Todo List](#7-master-todo-list)
8. [Technical Stories](#8-technical-stories)
9. [Test Coverage Strategy](#9-test-coverage-strategy)

---

## 1. Product & Audience Context

**Cipher** is a real-time options flow intelligence platform. It ingests live Tradier option tick data, classifies trades by tier (T1/T2/T3), aggregates them into episodes, runs a 6-agent AI swarm simulation, and surfaces composite signals for actionable trade decisions.

**Primary Users:**
- Sophisticated retail traders who watch unusual options activity (UOA)
- Semi-institutional desks running flow-based strategies
- Power users who operate during market hours (9:30 AM – 4:00 PM ET) and expect Bloomberg-Terminal-grade data density

**User Expectations:**
- Data is live — staleness is a cardinal sin. Every component must communicate freshness clearly.
- Speed to signal — a trader seeing a sweep hit the flow table needs to reach the composite score in ≤2 clicks.
- Trust — professional aesthetics signal that the data pipeline is reliable.
- Mobile checks — traders glance at their phones between positions; the mobile experience must be usable, not just accessible.

---

## 2. Current State Audit

### Dashboard (`/dashboard/page.tsx`)

| Dimension | Current State | Problem |
|---|---|---|
| Architecture | Single 300-line monolithic page component | Impossible to test, maintain, or code-split |
| Navigation | Flat tab bar with unicode icon glyphs | Cryptic icons, no grouping, doesn't scale past 6 tabs |
| Loading states | Plain `"Loading…"` text strings | No skeleton shimmer — layout shift on data arrival |
| Data freshness | Countdown text only | No visual pulse/indicator; traders can't tell at a glance |
| Mobile | Basic md: breakpoints | Stats bar overflows on small screens; tab bar scrolls horizontally with no affordance |
| Error handling | Silent catch blocks, no toasts | User has no feedback when fetch fails |
| State management | Raw useState + setInterval | No cache, no deduplication, repeated fetches on tab switch |
| Ticker search | Defined inline in page.tsx | Not reusable, not testable |
| Empty states | Plain text | No actionable prompts |
| Virtualization | None | Flow Events can have thousands of rows — browser freezes |
| Market context | Absent | No SPY/VIX/VIX-term bar, no market-open status |

### Admin (`/admin/page.tsx`)

| Dimension | Current State | Problem |
|---|---|---|
| Architecture | Single 700-line monolithic file with inline sub-components | All logic, UI, and data fetching co-located |
| Design tokens | Hardcoded hex palette `const A = {...}` | Diverges from dashboard CSS vars — two separate design systems |
| Tier Thresholds | Flat labeled input rows for all 15 fields | No visual grouping by tier — cognitively overwhelming |
| Stream Health | Static stat boxes | No trend arrows, no delta vs. previous poll, no color-coded thresholds |
| Demo Engine toggle | No confirmation dialog | Operator can accidentally flip production stream |
| Notifications | None | No toast on save success/error/failure |
| Breadcrumb | None | No sense of location; just a back button |
| Activity log | Absent | No audit trail for who changed what |
| Mobile | `p-8` fixed padding, 2-column grid | Breaks on mobile — form fields overflow |
| Validation | Client-side only, inline error span | No debounce, no type-safe validation (no Zod) |

---

## 3. PUD Design Vision

### 3.1 Design Principles

1. **Data Clarity First** — Every number must carry context: is it live? Is it rising? What is the reference range?
2. **Operational Confidence** — Operators using Admin must always know system state before acting. Confirmation = safety net.
3. **Peripheral Awareness** — Key system health signals must be visible without navigating away from the current task.
4. **Progressive Disclosure** — Show summary → let user drill in. Never dump all fields at once.
5. **Consistent Motion** — Transitions signal state changes (data loading in, toast appearing, tab switching). No janky layout shifts.

### 3.2 Dashboard Revamp

#### Navigation: Sidebar (replaces Tab Bar)

Replace the horizontal tab bar with a **collapsible left sidebar**:

```
┌─────────────────────────────────────────────────────────┐
│ ≡  CIPHER           [● LIVE]  [Symbols: 4,279]  [OPEN]  │  ← Top Header
├──────┬──────────────────────────────────────────────────┤
│  ⟁   │                                                  │
│  ◎   │   Main Content Area (active tab panel)           │
│  ◉ 3 │                                                  │  ← Badge counts
│  ⬡   │                                                  │
│  ◈   │                                                  │
│  🕐  │                                                  │
│      │                                                  │
│ ─────│                                                  │
│  ⚙   │ (Admin link — admin users only)                  │
│  👤  │                                                  │
│  ◑   │ (Theme toggle)                                   │
└──────┴──────────────────────────────────────────────────┘
```

- **Desktop**: Sidebar is 56px (icon only) by default, expands to 200px on hover/keyboard focus
- **Mobile**: Sidebar collapses to a bottom tab bar (5 primary tabs max, "More" overflow)
- **Badge**: Live signal count on the Signals tab icon — animates in with a scale pop

#### Market Pulse Bar (new)

A slim banner below the header showing:
```
Market: OPEN  |  Stream: ● LIVE  |  Active Symbols: 4,279  |  Signals Today: 142
```
> ⚠️ **Scope constraint:** SPY / QQQ / VIX index price fields are intentionally excluded. They require a new external market data API dependency (Polygon, Yahoo Finance, etc.) which is out of scope at this time. These fields can be added in a future story once an index-quote source is integrated into the backend. All four fields above are sourced exclusively from existing endpoints.
- Color-coded: green = market open, red = closed/pre-market, amber = pre/after-hours
- Stream pill pulses green when ticks are arriving (CSS animation)
- Collapses to a single icon row on mobile

#### Per-Tab Improvements

**Flow Events tab:**
- Sticky column headers with sort controls
- Virtualized row rendering (react-virtual) — handles 10k+ rows without freeze
- Filter sidebar (collapsible): ticker, contract type, tier, size range, date range
- Row color coding: T1 = cyan accent, T2 = indigo accent, T3 = amber accent
- "Jump to live" button when scrolled up in history
- Skeleton row shimmer on load

**Flow Episodes tab:**
- Episode cards instead of table rows (better mobile experience)
- Episode timeline view option
- Confidence bar as a visual progress indicator, not just a number

**Live Signals tab:**
- WebSocket connection status: animated green dot (pulse) = connected, red = disconnected
- Signal cards with colored verdict badge (BUY=green, SELL=red, HOLD=amber)
- Auto-scroll to latest with a "pause scroll" button
- Signal count badge on tab icon updates in real time

**AI Simulation tab:**
- Agent cards showing each of the 6 agents with their verdict + confidence
- Animated progress bar during run
- Verdict display: large, prominent, color-coded

**Composite tab:**
- Search bar always visible at top of content (not buried in header area)
- Score breakdown: horizontal stacked bar (flow × 0.55 / backtest × 0.35 / volume × 0.10)
- Historical composite scores sparkline

**Signal History tab:**
- Timeline view with grouped-by-day sections
- Export to CSV button

#### Skeleton Loading System

Every data panel gets a skeleton shimmer that matches the shape of the loaded content:
- Tables: 8 skeleton rows with column-width pulse bars
- Stat grids: Rectangular shimmer blocks
- Cards: Full card outline with inner content shimmer

### 3.3 Admin Revamp

#### Layout

```
┌─────────────────────────────────────────────────────────┐
│ Admin › Control Panel            [← Dashboard]  [email]  │
├─────────────────────────────────────────────────────────┤
│  [Stream Health]  [Demo Engine]  [Config]  [Registry]   │  ← Admin Tab Nav
├─────────────────────────────────────────────────────────┤
│                                                         │
│   Content for selected admin section                    │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

Admin gets its own internal tab navigation (not sidebar) because it is an operator tool with distinct functional sections.

#### Stream Health section
- **Live counters** with delta arrows: Ticks ▲1,234 vs. previous poll
- **Color thresholds**: Error count > 0 → red pill, reconnects > 2 → amber, else green
- **Uptime progress ring** (circular SVG, not just text)
- **Trend sparklines** for ticks-per-second (last 10 polls)

#### Demo Engine section
- **Modal confirmation** before start/stop: "You are about to START the demo engine. This will inject synthetic flow. Confirm?"
- **Live stats counters** that animate as ticks emit (count-up animation)
- **Auto-stop timer**: Show remaining time if there's a TTL on the demo session

#### Tier Thresholds section
- **Three tier cards** side by side (T1 / T2 / T3), each card contains all 5 fields for that tier
- **Batch save button** per tier card (not per-field individual saves)
- **Visual diff**: When you edit a field, show the original value in muted text below
- **Zod validation** before any save attempt

#### Ingestion Config section
- **Grouped by prefix** (e.g., `STREAM_*`, `UNIVERSE_*`, `SIGNAL_*`)
- **Type badges** next to field names (STRING, INTEGER, BOOLEAN)
- **Change history** inline: "Last changed by admin@cipher.io at 2:30 PM"

#### Pipeline Overview section
- Animated flow diagram (CSS transitions, not static text list)
- Each step shows live count (e.g., "4,279 symbols in universe")

#### Activity Log (new section)
- Last 50 config changes with timestamp, actor, field, old value → new value

---

## 4. PFE Architecture Plan

### 4.1 Folder Structure (new)

```
frontend/src/
├── app/
│   ├── dashboard/
│   │   └── page.tsx                    ← thin orchestrator only
│   ├── admin/
│   │   └── page.tsx                    ← thin orchestrator only
│   └── globals.css
├── components/
│   ├── ui/                             ← DESIGN SYSTEM primitives
│   │   ├── Skeleton.tsx
│   │   ├── Toast.tsx
│   │   ├── Modal.tsx
│   │   ├── Badge.tsx
│   │   ├── Button.tsx
│   │   ├── Card.tsx
│   │   ├── Stat.tsx
│   │   ├── Spinner.tsx
│   │   ├── Tooltip.tsx
│   │   ├── Tabs.tsx
│   │   └── Input.tsx
│   ├── layout/
│   │   ├── AppSidebar.tsx
│   │   ├── AppHeader.tsx
│   │   ├── MarketPulseBar.tsx
│   │   ├── MobileBottomNav.tsx
│   │   └── AdminHeader.tsx
│   ├── dashboard/
│   │   ├── FlowEventsTab.tsx           ← existing, refactored
│   │   ├── FlowEpisodesTab.tsx         ← existing, refactored
│   │   ├── SignalFeed.tsx              ← existing, refactored
│   │   ├── SimulationPanel.tsx         ← existing, refactored
│   │   ├── CompositeCard.tsx           ← existing, refactored
│   │   ├── SignalHistory.tsx           ← existing, refactored
│   │   ├── VirtualFlowTable.tsx        ← NEW: react-virtual
│   │   ├── FlowEventRow.tsx            ← NEW: memoized row
│   │   ├── FlowFilters.tsx             ← NEW: filter sidebar
│   │   ├── MarketPulseBar.tsx          ← NEW
│   │   ├── TickerSearchBar.tsx         ← extracted from page.tsx
│   │   └── SignalBadge.tsx             ← NEW
│   └── admin/
│       ├── StreamHealthCard.tsx        ← extracted, enhanced
│       ├── DemoEngineCard.tsx          ← extracted, enhanced
│       ├── TierThresholdsCard.tsx      ← extracted, enhanced
│       ├── IngestionConfigCard.tsx     ← extracted, enhanced
│       ├── PipelineOverview.tsx        ← extracted, enhanced
│       ├── TierDistributionCard.tsx    ← extracted, enhanced
│       ├── ActivityLog.tsx             ← NEW
│       └── ConfirmModal.tsx            ← NEW
├── hooks/
│   ├── useAuth.ts                      ← existing
│   ├── useFlow.ts                      ← existing
│   ├── useFlowEvents.ts                ← existing, add SWR
│   ├── useFlowEpisodes.ts              ← existing, add SWR
│   ├── useSignalStream.ts              ← existing
│   ├── useSimulation.ts                ← existing
│   ├── useAdminDemo.ts                 ← existing
│   ├── useStreamHealth.ts              ← NEW: SWR-based with delta tracking
│   ├── useMarketStatus.ts              ← NEW: open/closed/pre/after
│   ├── useToast.ts                     ← NEW: toast context hook
│   └── useVirtualList.ts               ← NEW: wrapper for @tanstack/virtual
├── lib/
│   ├── api.ts                          ← existing
│   ├── validators.ts                   ← NEW: Zod schemas for admin forms
│   └── formatters.ts                   ← NEW: shared number/time formatters
├── context/
│   └── ToastContext.tsx                ← NEW
└── types/
    └── index.ts                        ← existing, expand
```

### 4.2 State Management

**Replace raw useState+setInterval with SWR:**
- `useSWR` for all polled data (stream health, flow events, flow episodes, tier distribution)
- `useSWRMutation` for admin saves (PATCH operations)
- Eliminates manual `useEffect` cleanup bugs
- Automatic deduplication of concurrent requests
- Background revalidation on window focus

**WebSocket signals:** Keep existing `useSignalStream` hook — WebSocket is the right primitive here. Enhance to auto-reconnect with exponential backoff and expose connection quality metrics.

### 4.3 Performance Requirements

| Metric | Target | Mechanism |
|---|---|---|
| LCP (Largest Contentful Paint) | < 1.2s | next/dynamic code splitting per tab |
| Flow Events table (10k rows) | < 16ms frame time | @tanstack/react-virtual |
| Tab switch | < 100ms | Tabs keep DOM alive (CSS visibility), not unmount/remount |
| Admin page load | < 800ms | Parallel SWR fetches |
| Mobile Lighthouse score | ≥ 90 | Responsive design + image optimization |

### 4.4 Dependency Additions

```json
{
  "swr": "^2.x",
  "@tanstack/react-virtual": "^3.x",
  "zod": "^3.x",
  "clsx": "^2.x",
  "react-hot-toast": "^2.x"
}
```

No heavy component libraries (shadcn is fine for primitives if already present). Keep bundle lean.

### 4.5 CSS Architecture

Centralize all design tokens in `globals.css` under `:root` and `[data-theme="dark"]`. Remove the hardcoded `const A = {...}` palette in admin/page.tsx. Map all components to CSS variables only.

---

## 5. Design System Tokens

```css
/* globals.css additions */
:root {
  /* Spacing scale */
  --space-1: 0.25rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-6: 1.5rem;
  --space-8: 2rem;

  /* Typography */
  --font-mono: "JetBrains Mono", "Fira Code", ui-monospace, monospace;
  --font-sans: Inter, ui-sans-serif, system-ui, sans-serif;
  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-base: 1rem;
  --text-lg: 1.125rem;
  --text-xl: 1.25rem;
  --text-2xl: 1.5rem;

  /* Tier colors */
  --tier-1: var(--cyan);
  --tier-1-dim: var(--cyan-dim);
  --tier-1-border: var(--cyan-border);
  --tier-2: var(--indigo);
  --tier-2-dim: var(--indigo-dim);
  --tier-2-border: var(--indigo-border);
  --tier-3: var(--amber);
  --tier-3-dim: var(--amber-dim);
  --tier-3-border: var(--amber-border);

  /* Signal verdict colors */
  --verdict-buy: var(--green);
  --verdict-sell: var(--red);
  --verdict-hold: var(--amber);

  /* Shadows */
  --shadow-card: 0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.4);
  --shadow-elevated: 0 4px 16px rgba(0,0,0,0.5);

  /* Animation durations */
  --duration-fast: 100ms;
  --duration-normal: 200ms;
  --duration-slow: 350ms;

  /* Border radii */
  --radius-sm: 0.375rem;
  --radius-md: 0.5rem;
  --radius-lg: 0.75rem;
  --radius-xl: 1rem;
}
```

---

## 6. Component Decomposition Map

### Dashboard page.tsx (before → after)

| Before | After | Notes |
|---|---|---|
| 300-line monolith | ~60-line orchestrator | Delegates everything to layout + tab components |
| TickerSearchBar defined inline | `components/dashboard/TickerSearchBar.tsx` | Standalone, testable |
| setInterval for stats | `useSWR` in `useStreamStats` hook | Auto-dedup, cache, revalidate |
| Tab state in page | URL-based `?tab=flow_events` | Deep-linkable, shareable |
| Raw FlowTable | `VirtualFlowTable` with react-virtual | Handles 10k+ rows |

### Admin page.tsx (before → after)

| Before | After | Notes |
|---|---|---|
| 700-line monolith with 8 inline components | 1 thin page + 8 separate component files | Each independently testable |
| `const A = {...}` hex palette | CSS variables only | Unified with dashboard design system |
| Per-field Save buttons | Per-tier Batch Save | Better UX, fewer API calls |
| No confirmation on Demo Engine toggle | `ConfirmModal` | Prevents accidental prod disruption |
| No toast notifications | `react-hot-toast` | Universal feedback |
| No activity log | `ActivityLog` component | Operator accountability |

---

## 7. Master Todo List

### Phase 0 — Foundation (must complete before any UI work)

- [ ] **P0-1** Install new dependencies: `swr`, `@tanstack/react-virtual`, `zod`, `clsx`, `react-hot-toast`
- [ ] **P0-2** Extend `tailwind.config.ts` with design token utilities (spacing, font-size scale, animation)
- [ ] **P0-3** Migrate all hardcoded hex values from `admin/page.tsx` to CSS custom properties in `globals.css`
- [ ] **P0-4** Create `src/context/ToastContext.tsx` and wrap root `layout.tsx` with `<Toaster />`
- [ ] **P0-5** Create `src/lib/validators.ts` with Zod schemas for `TierThresholdsRow` and `ConfigRow`
- [ ] **P0-6** Create `src/lib/formatters.ts` with shared `fmtUptime`, `fmtTime`, `fmtNumber`, `fmtPct` utilities
- [ ] **P0-7** Create `src/types/index.ts` central type exports (consolidate all scattered interface declarations)
- [ ] **P0-8** Update Jest config to enforce 100% coverage threshold on all new files in `components/ui/`, `components/layout/`, `hooks/`
- [ ] **P0-9** Audit and update all existing tests to pass against the new design system tokens

### Phase 1 — UI Primitives / Design System

- [ ] **P1-1** `components/ui/Skeleton.tsx` — animated shimmer component with configurable width/height/rounded
- [ ] **P1-2** `components/ui/Badge.tsx` — status badge with variant support (success, warning, error, info, tier-1/2/3)
- [ ] **P1-3** `components/ui/Button.tsx` — primary / secondary / ghost / destructive variants, loading spinner state
- [ ] **P1-4** `components/ui/Card.tsx` — base card with optional header slot, footer slot, padding variants
- [ ] **P1-5** `components/ui/Stat.tsx` — metric display with label, value, optional delta arrow (▲/▼) and color
- [ ] **P1-6** `components/ui/Modal.tsx` — accessible dialog with backdrop, close button, focus trap
- [ ] **P1-7** `components/ui/Input.tsx` — text input with dirty/error/saved state styles matching design tokens
- [ ] **P1-8** `components/ui/Spinner.tsx` — SVG spinner with size variants
- [ ] **P1-9** `components/ui/Tooltip.tsx` — hover tooltip using CSS custom properties
- [ ] **P1-10** `components/ui/Tabs.tsx` — accessible tab list/panel pair with keyboard navigation (ArrowLeft/ArrowRight)
- [ ] **P1-11** Write 100% unit tests for all P1 components

### Phase 2 — Layout Components

- [ ] **P2-1** `components/layout/AppSidebar.tsx` — collapsible sidebar (56px icon / 200px expanded), with nav items, badge support, admin link conditional on `isAdmin`
- [ ] **P2-2** `components/layout/AppHeader.tsx` — top bar: logo, market pulse pill, theme toggle, user email, sign out — replaces current header
- [ ] **P2-3** `components/layout/MarketPulseBar.tsx` — slim banner: market status (OPEN/CLOSED/PRE/AFTER, derived from ET clock only), stream mode pulse dot, active symbol count, signals-today count — all from existing endpoints. ⚠️ No SPY/QQQ/VIX fields — deferred until a market data source is integrated
- [ ] **P2-4** `components/layout/MobileBottomNav.tsx` — 5-item bottom tab bar for viewport < 768px, replaces sidebar on mobile, badge count support
- [ ] **P2-5** `components/layout/AdminHeader.tsx` — admin top bar: breadcrumb (Admin > Control Panel), back-to-dashboard button, user email
- [ ] **P2-6** Refactor `app/dashboard/page.tsx` to use new layout: `<AppHeader>` + `<AppSidebar>` + `<MarketPulseBar>` + URL-driven tab routing (`?tab=`)
- [ ] **P2-7** Write 100% unit tests for all P2 components

### Phase 3 — Dashboard Tab Components (Refactor + Enhance)

- [ ] **P3-1** Extract `TickerSearchBar` from `dashboard/page.tsx` → `components/dashboard/TickerSearchBar.tsx`, add debounce, uppercase enforcement, clear button
- [ ] **P3-2** `components/dashboard/VirtualFlowTable.tsx` — virtualized table using `@tanstack/react-virtual`, memoized `FlowEventRow`, handles 10k+ rows, sticky headers
- [ ] **P3-3** `components/dashboard/FlowEventRow.tsx` — memoized (`React.memo`) row component with tier color coding, size formatting, time formatting
- [ ] **P3-4** `components/dashboard/FlowFilters.tsx` — collapsible filter sidebar: ticker, contract type (CALL/PUT), tier (1/2/3), size range slider, DTE range, date range picker
- [ ] **P3-5** Refactor `FlowEventsTab.tsx` to use `VirtualFlowTable` + `FlowFilters` + `Skeleton` rows + error boundary
- [ ] **P3-6** `components/dashboard/SignalBadge.tsx` — verdict badge: BUY (green), SELL (red), HOLD (amber) with icon and confidence score
- [ ] **P3-7** Refactor `SignalFeed.tsx` — signal cards with `SignalBadge`, animated auto-scroll, "Pause / Resume" scroll lock button, connection status pulse dot
- [ ] **P3-8** Refactor `SimulationPanel.tsx` — agent cards grid (6 agents), animated progress bar, large verdict display with SignalBadge
- [ ] **P3-9** Refactor `CompositeCard.tsx` — stacked bar score breakdown, historical sparkline placeholder, ticker always-visible search bar
- [ ] **P3-10** Refactor `SignalHistory.tsx` — grouped-by-day sections, "Export CSV" button, skeleton loading
- [ ] **P3-11** Refactor `FlowEpisodesTab.tsx` — episode summary cards, confidence visual bar, skeleton loading
- [ ] **P3-12** Replace `StreamStatsBar` in header with `MarketPulseBar` below header
- [ ] **P3-13** Migrate all dashboard data fetching to SWR hooks (`useFlowEvents`, `useFlowEpisodes`) — remove manual setInterval patterns
- [ ] **P3-14** Add URL-based tab routing: `?tab=flow_events` as default, update on tab switch, read on mount
- [ ] **P3-15** Add error boundaries at page and per-tab level with graceful fallback UI
- [ ] **P3-16** Write 100% unit tests for all P3 components (including VirtualFlowTable with mocked data)

### Phase 4 — Admin Page Components (Refactor + Enhance)

- [ ] **P4-1** Extract `StreamHealthCard.tsx` — add delta arrows on counters (compare to previous poll), color-threshold rules, uptime SVG ring, ticks-per-second sparkline (last 10 data points), `Skeleton` loading state
- [ ] **P4-2** Extract `DemoEngineCard.tsx` — add `ConfirmModal` before start/stop, animated counter during active demo, auto-stop countdown display
- [ ] **P4-3** `components/admin/ConfirmModal.tsx` — reusable confirmation dialog: title, body, confirm label (customizable), cancel button, destructive variant (red confirm button)
- [ ] **P4-4** Extract `TierThresholdsCard.tsx` — redesign as 3 tier cards side by side (T1/T2/T3), each card with 5 fields, per-card "Save Tier" batch button, Zod validation, original-value diff display, toast on save
- [ ] **P4-5** Extract `IngestionConfigCard.tsx` — group config rows by key prefix, type badges (STRING/INTEGER/BOOLEAN/FLOAT), inline change history display, toast on save
- [ ] **P4-6** `components/admin/ActivityLog.tsx` — NEW: table of last 50 config changes: timestamp, actor email, field name, old value → new value; fetched from `/api/admin/activity-log`
- [ ] **P4-7** Enhance `TierDistributionCard.tsx` — visual bar charts for tier symbol counts (pure CSS/Tailwind), OI sample list with search/filter, loading skeleton
- [ ] **P4-8** Enhance `PipelineOverview.tsx` — animated CSS step flow diagram, each step shows live count pulled from stream health data
- [ ] **P4-9** Refactor `AdminPage` orchestrator — use Admin internal tab nav (Stream Health / Demo Engine / Thresholds / Config / Distribution / Activity), responsive grid
- [ ] **P4-10** Migrate all admin data fetching to SWR (`useStreamHealth` with delta tracking, `useTierThresholds`, `useIngestionConfig`, `useTierDistribution`)
- [ ] **P4-11** Add mobile responsive layout: single-column on mobile, 2-column on lg, admin tab nav collapses to dropdown on mobile
- [ ] **P4-12** Add toast notifications (react-hot-toast) for all save/error/success events on admin page
- [ ] **P4-13** Write 100% unit tests for all P4 components

### Phase 5 — New Hooks

- [ ] **P5-1** `hooks/useStreamHealth.ts` — SWR-based, 10s revalidation, computes delta vs. previous poll for each counter, exposes `health`, `delta`, `loading`, `error`
- [ ] **P5-2** `hooks/useMarketStatus.ts` — returns `{ status: "open"|"closed"|"pre"|"after", marketOpenAt, marketCloseAt }` based on ET time — pure computed, no API call
- [ ] **P5-3** `hooks/useToast.ts` — thin wrapper around `react-hot-toast` `toast()` for consistent styling across app
- [ ] **P5-4** `hooks/useVirtualList.ts` — typed wrapper around `@tanstack/react-virtual` `useVirtualizer` with sensible defaults for the Flow Events table
- [ ] **P5-5** `hooks/useTierThresholds.ts` — SWR-based, exposes `data`, `loading`, `error`, `mutate`, `save(field, value)` with optimistic update
- [ ] **P5-6** `hooks/useIngestionConfig.ts` — SWR-based, similar to useTierThresholds
- [ ] **P5-7** `hooks/useTierDistribution.ts` — SWR-based, 60s revalidation
- [ ] **P5-8** Write 100% unit tests for all P5 hooks (mock SWR, mock fetch)

### Phase 6 — Performance & Quality

- [ ] **P6-1** Add `next/dynamic` code-splitting for `SimulationPanel`, `SignalHistory`, `CompositeCard` (heavy components, not needed on initial paint)
- [ ] **P6-2** Add `React.memo` to `FlowEventRow`, `SignalBadge`, `Stat`, `AdminCard` to prevent unnecessary re-renders
- [ ] **P6-3** Add `useMemo` for filtered/sorted Flow Events data before passing to VirtualFlowTable
- [ ] **P6-4** Tab panels: use CSS `visibility: hidden / display: none` toggle rather than conditional rendering to preserve scroll position on tab switch
- [ ] **P6-5** Lighthouse audit: target ≥ 90 mobile score — fix any blocking render issues
- [ ] **P6-6** Add `loading="lazy"` and proper `alt` attributes to any images (CipherLogo SVG optimization)
- [ ] **P6-7** Add `<meta>` tags and viewport config for mobile in `layout.tsx`

### Phase 7 — Testing Completion

- [ ] **P7-1** Update `__tests__/dashboard.test.tsx` — rewrite for new sidebar layout, URL-based tab routing, SWR mocks
- [ ] **P7-2** Update `__tests__/admin.test.tsx` — rewrite for new component structure, ConfirmModal flow, toast assertions
- [ ] **P7-3** Add integration tests: full Dashboard page render with all tabs navigable, all hooks mocked
- [ ] **P7-4** Add integration tests: full Admin page render, Demo Engine start/stop with modal confirmation, Tier Threshold save with toast
- [ ] **P7-5** Enforce coverage thresholds in `jest.config.js`: `{ branches: 100, functions: 100, lines: 100, statements: 100 }` for all new files
- [ ] **P7-6** Add snapshot tests for all UI primitive components (Skeleton, Badge, Button, Card, Modal, Stat)
- [ ] **P7-7** Add accessibility tests: verify keyboard navigation on sidebar, modal focus trap, tab panel ARIA attributes

---

## 8. Technical Stories

> Format: **Story ID** | Title | Description | Acceptance Criteria | Test Requirements | Estimate

---

### STORY-001 | Dependency & Foundation Setup
**Phase:** P0
**Description:** Install all new dependencies and set up the foundational infrastructure required for the revamp. No UI changes, purely scaffolding.
**Acceptance Criteria:**
- `swr`, `@tanstack/react-virtual`, `zod`, `clsx`, `react-hot-toast` installed and importable
- `ToastContext.tsx` created and `<Toaster />` mounted in root `layout.tsx`
- `tailwind.config.ts` extended with spacing, font, animation token utilities
- All hardcoded hex values in `admin/page.tsx` replaced with CSS variable references
- `globals.css` contains full design token set including tier colors, verdict colors, shadow, radius, and animation duration variables
- `formatters.ts` exports: `fmtUptime(secs)`, `fmtTime(iso)`, `fmtNumber(n)`, `fmtPct(n)`
- `validators.ts` exports: `TierThresholdsSchema` (Zod), `ConfigRowSchema` (Zod)
- `types/index.ts` consolidates all scattered interface declarations from both page files

**Test Requirements:**
- Unit tests for all `formatters.ts` functions (edge cases: null, zero, very large numbers, negative)
- Unit tests for all `validators.ts` Zod schemas (valid inputs, invalid inputs, boundary values)
- Jest coverage threshold added for new files: 100% lines/branches/functions/statements

**Estimate:** 0.5 day

---

### STORY-002 | UI Primitive: Skeleton Component
**Phase:** P1
**Description:** Build a reusable shimmer skeleton component that matches the shape of the content being loaded. Used across all data panels on both Dashboard and Admin pages.
**Acceptance Criteria:**
- `<Skeleton width height rounded className />` renders an animated pulse div
- Supports `rounded` prop variants: `none`, `sm`, `md`, `lg`, `full`
- Animation uses `animate-pulse` Tailwind class with CSS variable `--surface-2` background
- `<Skeleton.Text lines={n} />` renders n lines of skeleton text with variable widths (last line 60% width)
- `<Skeleton.Row columns={n} />` renders a skeleton table row with n cells

**Test Requirements:**
- Renders without crashing with all prop combinations
- Snapshot test for `Skeleton`, `Skeleton.Text`, `Skeleton.Row`
- `data-testid="skeleton"` present for query in integration tests
- Validates `rounded` prop type — TypeScript compile check

**Estimate:** 0.25 day

---

### STORY-003 | UI Primitive: Badge Component
**Phase:** P1
**Description:** Status badge used for tier labels, verdict indicators, stream mode, admin section labels.
**Acceptance Criteria:**
- Variants: `success`, `warning`, `error`, `info`, `tier-1`, `tier-2`, `tier-3`, `neutral`
- Each variant maps to correct CSS variable colors (no hardcoded hex)
- Size variants: `sm`, `md`
- Optional pulsing dot animation on `live` prop
- Accessible: uses `role="status"` when `live` prop is true

**Test Requirements:**
- Snapshot test for each variant
- `live` prop triggers CSS animation class
- Color mapping verified for all variants
- 100% branch coverage on variant logic

**Estimate:** 0.25 day

---

### STORY-004 | UI Primitive: Button Component
**Phase:** P1
**Description:** Unified button component replacing all inline button styles across both pages.
**Acceptance Criteria:**
- Variants: `primary`, `secondary`, `ghost`, `destructive`
- Size variants: `sm`, `md`, `lg`
- `loading` prop: shows `<Spinner />` and disables button, preserves button width
- `disabled` state: correct cursor and opacity
- All variants use CSS variables only
- Fully typed with TypeScript, extends `React.ButtonHTMLAttributes<HTMLButtonElement>`

**Test Requirements:**
- Renders each variant correctly (snapshot)
- Loading state: spinner visible, button disabled, click handler not called
- Disabled state: click handler not called
- onCLick fires on enabled state
- 100% coverage

**Estimate:** 0.25 day

---

### STORY-005 | UI Primitive: Modal Component
**Phase:** P1
**Description:** Accessible confirmation/alert modal used for Demo Engine toggle confirmation and any future destructive actions.
**Acceptance Criteria:**
- Renders as a portal via `ReactDOM.createPortal` into `document.body`
- Backdrop click closes modal (unless `disableBackdropClose` prop)
- Escape key closes modal
- Focus is trapped inside modal when open (first focusable element receives focus on open)
- Returns focus to trigger element on close
- `variant` prop: `default`, `destructive` (red confirm button)
- Slots: `title`, `body`, `confirmLabel`, `cancelLabel`, `onConfirm`, `onCancel`
- Animated: fade-in with scale-up, fade-out on close (CSS transition)
- ARIA: `role="dialog"`, `aria-modal="true"`, `aria-labelledby`

**Test Requirements:**
- Opens/closes correctly
- Escape key triggers `onCancel`
- Backdrop click triggers `onCancel` (when enabled)
- Confirm button triggers `onConfirm`
- Focus trap: Tab key stays within modal
- Snapshot test for both variants
- `disableBackdropClose` prevents backdrop close
- 100% coverage

**Estimate:** 0.5 day

---

### STORY-006 | Layout: AppSidebar
**Phase:** P2
**Description:** Replace the horizontal tab bar on the Dashboard with a collapsible left sidebar that scales with future feature additions.
**Acceptance Criteria:**
- Desktop (≥ 1024px): sidebar is 56px wide (icons only) by default, expands to 200px on hover (CSS transition) or on explicit toggle
- Mobile (< 768px): sidebar is hidden; replaced by `MobileBottomNav`
- Nav items: icon + label, configurable via `items` prop array
- Active item: highlighted with `var(--amber)` left border + dim background
- Badge: numeric count displayed on icon for items with `badge` prop
- Admin link: only rendered when `isAdmin` is true
- Bottom section: Theme toggle icon, User avatar/email (truncated), Sign out icon
- Keyboard accessible: all items focusable, Enter/Space activates

**Test Requirements:**
- Renders correct items based on `items` prop
- Admin link absent when `isAdmin=false`, present when `isAdmin=true`
- Active item highlights correct item based on `activeItem` prop
- Badge renders with correct count
- Sign out button calls `onSignOut` prop
- Snapshot test for desktop and mobile variants (using window resize mock)
- 100% coverage

**Estimate:** 0.75 day

---

### STORY-007 | Layout: MarketPulseBar
**Phase:** P2
**Description:** A slim contextual banner below the header showing market status, key index levels, stream health, and active symbol count.
**⚠️ Data Scope:** Fields are limited strictly to data already available from existing endpoints. SPY/QQQ/VIX index prices are explicitly excluded — they would require a new external market data API dependency which is out of scope. These can be added in a future story.

**Data Sources:**
- Market status → `useMarketStatus` hook (pure ET clock computation, zero API calls)
- Stream mode → `/api/health/stream` → `mode` field (already used by admin StreamHealthCard)
- Active Symbols → `/api/signals/stream/stats` → `active_symbols` (already in `StreamStats`)
- Signals Today → `/api/signals/stream/stats` → `signals` (already in `StreamStats`)

**Acceptance Criteria:**
- Market status pill: OPEN (green), CLOSED (muted), PRE-MARKET (amber), AFTER-HOURS (amber)
- Status derived from `useMarketStatus` hook (computed from ET time, no API call)
- Stream mode pill: LIVE (green pulse), DEMO (cyan), RECONNECTING (amber), STOPPED (muted)
- Active Symbols count: pulled from `stats.active_symbols` via prop
- Signals Today count: pulled from `stats.signals` via prop
- No SPY/QQQ/VIX fields — zero new external API dependencies introduced
- Collapses to icon-only row on mobile (< 640px)
- Height: 32px desktop, 28px mobile
- Does not block content — positioned as a flow element below header, not sticky

**Test Requirements:**
- Renders correct market status based on mocked `useMarketStatus`
- Stream mode pill renders correct variant for each mode (LIVE, DEMO, RECONNECTING, STOPPED)
- Active Symbols renders correctly with null/undefined fallback to "—"
- Signals Today renders correctly with null/undefined fallback to "—"
- No external API calls made by this component (assert fetch not called)
- Mobile: icon-only layout applied at sm breakpoint (mock `window.innerWidth`)
- Snapshot tests for all market status states
- 100% coverage

**Estimate:** 0.5 day

---

### STORY-008 | Dashboard: VirtualFlowTable
**Phase:** P3
**Description:** Replace the existing Flow Events table with a virtualized implementation capable of handling 10,000+ rows without frame drops.
**Acceptance Criteria:**
- Uses `@tanstack/react-virtual` `useVirtualizer` with `overscan=5`
- Only DOM-renders rows in the viewport + overscan
- Sticky column headers with sort controls (click to sort asc/desc)
- Sortable columns: Time, Ticker, Type, Strike, Expiry, Size, Price, Tier
- `FlowEventRow` is wrapped in `React.memo` to prevent re-renders when other rows update
- Tier color coding: T1=cyan left border, T2=indigo, T3=amber
- "Jump to latest" button appears when scrolled more than 5 rows from bottom
- Skeleton rows (8×) shown during loading
- Error state with retry button

**Test Requirements:**
- Renders correct number of rows for given data
- Sorting: clicking header sorts data correctly (asc/desc toggle)
- Memoization: FlowEventRow does not re-render when sibling row updates (use render counter mock)
- Skeleton rows render during `loading=true`
- Error state renders with retry callback
- "Jump to latest" button appears/disappears based on scroll position (mock IntersectionObserver)
- 100% coverage

**Estimate:** 1 day

---

### STORY-009 | Dashboard: FlowFilters Sidebar
**Phase:** P3
**Description:** Collapsible filter panel for Flow Events tab allowing traders to narrow down by ticker, contract type, tier, size, and date.
**Acceptance Criteria:**
- Collapsible: collapsed by default on mobile, expanded by default on desktop
- Filters: Ticker (text input), Contract Type (CALL/PUT/both toggle), Tier (1/2/3 checkboxes), Min Size (number input), Max DTE (number input), Date range (from/to date inputs)
- Filter state lifted to parent via `onFiltersChange` callback
- "Clear all" button resets all filters
- Active filter count badge shown on the collapse toggle button
- All filter changes debounced 200ms before triggering `onFiltersChange`

**Test Requirements:**
- Renders all filter controls
- Ticker input debounces correctly (timer mock)
- Contract type toggle updates filter state
- Tier checkboxes update filter state (all combinations)
- "Clear all" resets all filter fields
- Active filter count badge updates correctly
- 100% coverage

**Estimate:** 0.75 day

---

### STORY-010 | Dashboard: URL-Based Tab Routing
**Phase:** P3
**Description:** Make the active dashboard tab deep-linkable via URL query parameter `?tab=` so users can bookmark views and share links.
**Acceptance Criteria:**
- Default tab: `flow_events` — URL shows `?tab=flow_events`
- Tab switch updates URL without page reload (Next.js `router.push` with shallow)
- On page load, reads `?tab` param and activates correct tab
- Invalid `?tab` value falls back to `flow_events`
- Browser back/forward navigates between tabs
- Sidebar active item reflects URL tab state

**Test Requirements:**
- On mount with `?tab=signals`, Signals tab is active
- On mount with unknown `?tab=invalid`, Flow Events tab is active
- Clicking sidebar item updates URL param (mock `useRouter`)
- Browser back: tab reverts to previous (mock router history)
- 100% coverage

**Estimate:** 0.5 day

---

### STORY-011 | Admin: ConfirmModal Integration (Demo Engine)
**Phase:** P4
**Description:** Prevent accidental Demo Engine start/stop in production by requiring explicit confirmation via modal.
**Acceptance Criteria:**
- Clicking "Start" or "Stop" opens `<ConfirmModal>` (not immediately triggering the API call)
- Modal title: "Start Demo Engine?" / "Stop Demo Engine?"
- Modal body explains the consequence: "This will inject synthetic flow events into the pipeline."
- Stop variant uses `destructive` modal variant (red confirm button)
- Only on Confirm click does the `toggle()` API call fire
- Cancel closes modal with no action
- Toast: "Demo engine started" / "Demo engine stopped" on success
- Toast: error message on failure

**Test Requirements:**
- Clicking Start shows modal (toggle not called yet)
- Clicking modal Confirm calls `toggle(true)`
- Clicking modal Cancel: toggle not called, modal closed
- Success: toast displayed with correct message
- Error: error toast displayed
- Stop button uses destructive modal variant
- 100% coverage

**Estimate:** 0.5 day

---

### STORY-012 | Admin: Tier Thresholds Redesign (Batch Save + Validation)
**Phase:** P4
**Description:** Redesign the flat Tier Thresholds editor into three visual tier cards with grouped fields and per-tier batch save.
**Acceptance Criteria:**
- Three cards rendered side-by-side on desktop, stacked on mobile: T1 (cyan), T2 (indigo), T3 (amber)
- Each card shows 5 fields: Min Volume, Min Last Price, Min OI, ATM%, Max DTE
- Below each field: original value shown in muted text when draft differs
- Per-card "Save Tier N" button: sends all 5 fields in a single PATCH request
- Zod validation before save: all values must be non-negative numbers; ATM% must be ≤ 1.0; Max DTE must be integer
- Validation errors shown inline below the input field
- Toast on save success: "Tier 1 thresholds saved"
- Toast on save error with server message
- Dirty indicator: card header gets amber accent when any field in that tier is modified

**Test Requirements:**
- Each tier card renders all 5 fields with correct current values
- Editing a field marks card as dirty (amber header)
- Save button calls PATCH with all 5 fields for that tier
- Zod validation: invalid value shows inline error, PATCH not called
- Success toast rendered on save
- Error toast rendered on API error
- Original value shown below dirty field
- 100% coverage

**Estimate:** 1 day

---

### STORY-BE-001 | [BACKEND — BLOCKER for STORY-013] Activity Log API & DB Migration
**Phase:** P4 (must complete before STORY-013 can start)
**Owner:** Backend
**Description:** Create the database table and API endpoint that STORY-013 (Activity Log frontend component) depends on. This story has zero frontend work — it is purely a backend prerequisite.

**DB Migration required:**
```sql
-- Migration 015: admin_activity_log table
CREATE TABLE IF NOT EXISTS admin_activity_log (
  id          bigserial PRIMARY KEY,
  actor       text       NOT NULL,          -- email of the user who made the change
  field       text       NOT NULL,          -- config key or tier field name
  old_value   text,                         -- previous value (null on first set)
  new_value   text       NOT NULL,          -- new value after change
  changed_at  timestamptz DEFAULT now()
);

CREATE INDEX idx_activity_log_changed_at ON admin_activity_log (changed_at DESC);

ALTER TABLE admin_activity_log ENABLE ROW LEVEL SECURITY;
-- Admin-only read policy to be added via 015_activity_log_rls.sql
```

**Backend changes required:**
1. Every successful PATCH in `/api/admin/tier-thresholds` handler must INSERT a row into `admin_activity_log` (one row per changed field)
2. Every successful PATCH in `/api/admin/ingestion/config` handler must INSERT a row into `admin_activity_log`
3. New GET endpoint: `GET /api/admin/activity-log`
   - Auth: admin JWT required
   - Query params: `limit` (default 50, max 200), `since` (ISO date, optional)
   - Response: `{ log: [{ id, actor, field, old_value, new_value, changed_at }] }`

**Acceptance Criteria:**
- Migration applied to cipher-database with no errors
- PATCH to tier-thresholds logs a row per changed field in `admin_activity_log`
- PATCH to ingestion/config logs a row in `admin_activity_log`
- `GET /api/admin/activity-log` returns last 50 rows ordered by `changed_at DESC`
- `GET /api/admin/activity-log?since=2026-04-29` filters correctly
- Endpoint returns 401 for unauthenticated requests, 403 for non-admin users

**Test Requirements (backend):**
- Unit test: PATCH tier-thresholds inserts log row with correct actor/field/old/new values
- Unit test: PATCH ingestion/config inserts log row
- Unit test: GET returns rows ordered by changed_at DESC
- Unit test: GET respects `limit` and `since` params
- Unit test: GET returns 401 without token, 403 for non-admin token

**Estimate:** 0.5 day (backend)

---

### STORY-013 | Admin: Activity Log (Frontend)
**Phase:** P4
**⚠️ BLOCKED BY: STORY-BE-001** — Do not start until `GET /api/admin/activity-log` endpoint is deployed.
**Description:** New admin section showing the last 50 configuration changes for operator accountability.
**Acceptance Criteria:**
- Fetches from `/api/admin/activity-log` with Authorization header
- Displays table: Timestamp | Actor | Field | Old Value | New Value
- Timestamp formatted as relative time (e.g., "3 minutes ago") with absolute time on hover tooltip
- Empty state: "No activity logged yet"
- Loading state: skeleton table rows
- Error state: error message with retry button
- Auto-refreshes every 60 seconds (SWR)
- Filtered by "Today" by default, with toggle for "All time"

**Test Requirements:**
- Renders table with correct columns
- Renders correct number of rows from mock data
- Loading state: skeleton rows visible
- Error state: error message and retry button visible
- Retry button triggers refetch
- Today filter applied by default, All Time toggle changes query
- Relative time formatting: "Just now", "5 minutes ago", "2 hours ago"
- Mock fetch: does not call API until STORY-BE-001 endpoint is available (use MSW mock during dev)
- 100% coverage

**Estimate:** 0.75 day (frontend)

---

### STORY-014 | Admin: Stream Health Delta Tracking
**Phase:** P4/P5
**Description:** Enhance the Stream Health card to show delta (change since last poll) for key counters, making it easier to see pipeline activity at a glance.
**Acceptance Criteria:**
- `useStreamHealth` hook tracks previous poll values
- Each counter shows delta: `▲ +234` (green) if positive, `▼ -1` (red) if negative, `—` if zero
- Errors counter: 0 = green badge, 1-2 = amber, >2 = red
- Reconnects counter: 0 = green, >0 = amber
- Uptime: SVG ring (0–24h scale) replaces plain text, text value shown in center
- Ticks-per-second sparkline: last 10 poll deltas plotted as a 60×20px SVG line chart
- Auto-refresh every 10s, last-refreshed timestamp shown

**Test Requirements:**
- Delta computed correctly: (current − previous) for each counter
- Delta arrow: positive = green ▲, negative = red ▼, zero = muted —
- Error threshold color: 0=green, 1=amber, 3=red (boundary tests)
- Uptime SVG: renders with correct arc path for given seconds value
- Sparkline: renders correct number of data points
- Mock SWR revalidation triggers delta recalculation
- 100% coverage

**Estimate:** 0.75 day

---

### STORY-015 | Mobile Responsiveness — Dashboard
**Phase:** P3/P6
**Description:** Ensure the Dashboard is fully usable on mobile (375px – 768px viewport).
**Acceptance Criteria:**
- `AppSidebar` hidden on mobile (<768px)
- `MobileBottomNav` rendered at bottom with 5 primary tabs (Flow Events, Episodes, Signals, Simulation, More)
- "More" tab opens a bottom sheet with remaining tabs (Composite, History)
- `MarketPulseBar` collapses to icon row on mobile
- Flow Events table: horizontal scroll on mobile, at minimum Ticker/Type/Size/Tier columns visible without scroll
- Filter sidebar: becomes a bottom sheet triggered by "Filter" button on mobile
- All touch targets: minimum 44×44px
- Admin page: all cards single-column on mobile, admin tab nav becomes a `<select>` dropdown

**Test Requirements:**
- Sidebar not rendered at <768px (mock viewport)
- MobileBottomNav rendered at <768px
- "More" bottom sheet opens and closes
- Touch targets ≥ 44px height (computed style check)
- Admin tab nav: select element rendered at <768px
- 100% coverage on MobileBottomNav, bottom sheet component

**Estimate:** 1 day

---

### STORY-016 | Performance: Dynamic Imports + Code Splitting
**Phase:** P6
**Description:** Use `next/dynamic` to lazy-load heavy components that are not needed on initial page paint.
**Acceptance Criteria:**
- `SimulationPanel` loaded with `next/dynamic` — not included in initial JS bundle
- `SignalHistory` loaded with `next/dynamic`
- `CompositeCard` loaded with `next/dynamic`
- `TierDistributionCard` (admin) loaded with `next/dynamic`
- Each dynamic import has a `loading` fallback using `<Skeleton>` of appropriate shape
- Bundle analyzer run to confirm chunk sizes are reduced
- No regression in tab-switch time (< 100ms first paint after lazy load)

**Test Requirements:**
- Dynamic imports render their loading state correctly
- Content renders after load (mock dynamic component resolution)
- 100% coverage on lazy wrapper components

**Estimate:** 0.5 day

---

### STORY-017 | Test Suite: Full Coverage Enforcement
**Phase:** P7
**Description:** Bring the entire new frontend codebase to 100% test coverage and update all existing tests to pass against the new design.
**Acceptance Criteria:**
- `jest.config.js` updated: `coverageThreshold.global` set to `{ branches: 100, functions: 100, lines: 100, statements: 100 }` scoped to `components/ui/**`, `components/layout/**`, `hooks/use*.ts`
- All existing tests in `__tests__/` updated to reflect new component names, prop APIs, and layout structure
- All new P1–P6 components have co-located test files
- No `@ts-ignore` or `eslint-disable` used to make tests pass
- CI: `npm test -- --coverage` must pass with zero failures in GitHub Actions

**Test Requirements (meta):**
- This story IS the testing story — acceptance criteria = CI green with 100% coverage
- Includes: unit tests, integration tests, snapshot tests, accessibility tests for all new components and hooks

**Estimate:** 1.5 days

---

## 9. Test Coverage Strategy

### Coverage Requirements

| Scope | Branch | Function | Line | Statement |
|---|---|---|---|---|
| `components/ui/**` | 100% | 100% | 100% | 100% |
| `components/layout/**` | 100% | 100% | 100% | 100% |
| `components/dashboard/**` (new/refactored) | 100% | 100% | 100% | 100% |
| `components/admin/**` (new/refactored) | 100% | 100% | 100% | 100% |
| `hooks/use*.ts` (new) | 100% | 100% | 100% | 100% |
| `lib/formatters.ts` | 100% | 100% | 100% | 100% |
| `lib/validators.ts` | 100% | 100% | 100% | 100% |
| Existing page files (after refactor) | ≥ 90% | ≥ 90% | ≥ 90% | ≥ 90% |

### Mocking Strategy

- **SWR**: Mock `swr` module with `jest.mock('swr')`, return controlled `{ data, error, isLoading, mutate }` objects
- **Next.js Router**: Mock `next/navigation` `useRouter`, `useSearchParams`, `usePathname`
- **`fetch`**: Mock `global.fetch` with `jest.fn()` per test
- **WebSocket**: Mock `WebSocket` constructor in `useSignalStream` tests
- **`window.innerWidth`**: Use `Object.defineProperty(window, 'innerWidth', ...)` for responsive tests
- **`IntersectionObserver`**: Mock in jsdom for scroll-position tests
- **`ReactDOM.createPortal`**: Mock to render inline for Modal tests

### Accessibility Tests

Use `@testing-library/jest-dom` + manual ARIA assertions:
- Sidebar: `role="navigation"`, all items `role="menuitem"`, active item `aria-current="page"`
- Modal: `role="dialog"`, `aria-modal="true"`, `aria-labelledby` pointing to title
- Tab panels: `role="tablist"`, `role="tab"`, `aria-selected`, `role="tabpanel"`, `aria-labelledby`
- Skeleton: `aria-busy="true"` on containing section
- Toast: `role="alert"`, `aria-live="polite"`

### Updating Existing Tests

Before implementing any Phase, run the full existing test suite and document which tests break. For each broken test:
1. If it tests removed code → delete the test
2. If it tests refactored code → update prop names, selectors, and mock shapes
3. If it tests moved code → update import paths only
4. Never delete a test just because it's hard to update — refactor it

---

## 10. Backend Dependencies

Only **one** backend story is required to complete this frontend revamp plan. All other stories (STORY-001 through STORY-017, excluding STORY-013) are entirely self-contained frontend work that can proceed immediately without any backend changes.

### Summary

| Backend Story | Blocks Frontend Story | Effort |
|---|---|---|
| STORY-BE-001 — Activity Log API + Migration | STORY-013 (Activity Log component) | 0.5 day |

### What Does NOT Need Backend Changes

- SWR migration — same existing endpoints, different client-side data-fetching pattern
- Sidebar, MarketPulseBar, MobileBottomNav — reads from existing `/api/health/stream`
- VirtualFlowTable + FlowFilters — same `/api/flow-events` response, client-side filtering
- All UI primitives (Skeleton, Badge, Button, Card, Modal, Input, Spinner, Tooltip, Tabs)
- ConfirmModal on Demo Engine — calls existing `toggle()` in `useAdminDemo`
- Tier Thresholds batch save — same PATCH endpoint, just sends all 5 fields at once instead of 1
- Stream Health delta tracking — delta computed client-side between SWR poll snapshots
- URL-based tab routing — Next.js client router only
- Dynamic imports / code splitting — Next.js build config only
- Mobile responsive layout — CSS/Tailwind only

### Sequencing Recommendation

Start STORY-001 through STORY-012 and STORY-014 through STORY-016 immediately in parallel with backend completing STORY-BE-001. STORY-013 is the only story that must wait. Use MSW (Mock Service Worker) in development to mock `GET /api/admin/activity-log` so the frontend component can be built and tested before the real endpoint is deployed.


---

## Appendix: Story Summary Table

| Story | Title | Phase | Days |
|---|---|---|---|
| STORY-001 | Dependency & Foundation Setup | P0 | 0.5 |
| STORY-002 | UI Primitive: Skeleton | P1 | 0.25 |
| STORY-003 | UI Primitive: Badge | P1 | 0.25 |
| STORY-004 | UI Primitive: Button | P1 | 0.25 |
| STORY-005 | UI Primitive: Modal | P1 | 0.5 |
| STORY-006 | Layout: AppSidebar | P2 | 0.75 |
| STORY-007 | Layout: MarketPulseBar | P2 | 0.5 |
| STORY-008 | Dashboard: VirtualFlowTable | P3 | 1.0 |
| STORY-009 | Dashboard: FlowFilters Sidebar | P3 | 0.75 |
| STORY-010 | Dashboard: URL-Based Tab Routing | P3 | 0.5 |
| STORY-011 | Admin: ConfirmModal (Demo Engine) | P4 | 0.5 |
| STORY-012 | Admin: Tier Thresholds Redesign | P4 | 1.0 |
| STORY-BE-001 | **[BACKEND BLOCKER]** Activity Log API & Migration | P4 | 0.5 (BE) |
| STORY-013 | Admin: Activity Log (Frontend) — blocked by STORY-BE-001 | P4 | 0.75 |
| STORY-014 | Admin: Stream Health Delta Tracking | P4/P5 | 0.75 |
| STORY-015 | Mobile Responsiveness — Dashboard | P3/P6 | 1.0 |
| STORY-016 | Dynamic Imports + Code Splitting | P6 | 0.5 |
| STORY-017 | Full Coverage Enforcement | P7 | 1.5 |
| **TOTAL** | | | **~11.5 days FE + 0.5 day BE** |
