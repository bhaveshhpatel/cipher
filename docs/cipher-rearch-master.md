# Cipher Re-Architecture Master Plan
**Branch:** `cipher-rearch` (aggregation) → `main` (when stable)  
**Strategy:** WSJ Steamroom signaling defaults · configurable via admin UI · index symbols permanently excluded · Vol/OI is a signal input, never a gate

---

## Architecture Overview

```
Tradier Stream
     │
     ▼
Gate 0: Index Symbol Filter (_INDEX_SYMBOLS, unconditional at registry + stream)
     │
Gate 1: Tier-Aware Min Premium (configurable per T1/T2/T3 via admin UI)
     │
Gate 2: Dedup Cache (configurable dedup_window_ms per tier)
     │
     ▼
flow_events persist ── chain_store.get(4-tuple) ──► contract_oi + contract_volume_snapshot (nullable, never a gate)
     │
     ▼
RepetitionAccumulator (window_minutes, dte_premium_tiers — configurable)
     │
     ▼
flow_episodes persist ─ chain_store.get(4-tuple) ──► contract_oi_at_open + contract_volume_at_close + volume_oi_ratio
     │
     ▼
Signal Gate: signal_min_premium + signal_debounce_ms (configurable via admin UI)
     │
     ▼
CompositeSignalEngine (WSJ Steamroom weights — all configurable via admin UI)
  ├── flow_score         × sig_weight_flow         (default 0.40)
  ├── premium_tier_score × sig_weight_premium_tier  (default 0.20)
  ├── vol_oi_factor      × sig_weight_vol_oi        (default 0.20)
  ├── backtest_score     × sig_weight_backtest       (default 0.10)
  └── multiday_bonus     × sig_weight_multiday       (default 0.10)
     │
     ▼
WebSocket Bus → Frontend Signal Cards (vol_oi_ratio badge + score breakdown)
```

### Chain Store (Background Service)
- Runs as a background asyncio task alongside the stream
- Refreshes all `stream_eligible` tracked symbols every 300s (configurable)
- Rate-limited: 0.6s inter-symbol sleep → ≤100 req/min at 60-symbol universe
- Scaling breakpoint: revisit cadence if symbol universe exceeds 200 symbols
- Daily reset at 09:29 ET — flushes prior-day volume
- Lookup: O(1) synchronous dict by `(symbol, expiration, strike, option_type)`
- Index symbols excluded from chain refresh unconditionally

---

## Story Sequence & Status

| # | Story ID | Title | Type | Status | Deliberation | Branch | Depends On |
|---|---|---|---|---|---|---|---|
| 1 | [REARCH-003](https://github.com/bhaveshhpatel/cipher/issues/92) | DB Schema Migration — Vol/OI Columns | DB Migration | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-003-schema-migration` | None |
| 2 | [REARCH-001](https://github.com/bhaveshhpatel/cipher/issues/90) | Chain Store Service — Background OI/Volume Cache | Backend | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-001-chain-store` | None |
| 3 | [REARCH-005](https://github.com/bhaveshhpatel/cipher/issues/94) | Index Exclusion Hardening — Registry + Watchlist | Backend | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-005-index-exclusion` | REARCH-001 |
| 4 | [REARCH-002](https://github.com/bhaveshhpatel/cipher/issues/91) | Flow Event Vol/OI Enrichment | Backend | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-002-event-enrichment` | REARCH-001, REARCH-003 |
| 5 | [REARCH-004](https://github.com/bhaveshhpatel/cipher/issues/93) | Episode-Level Vol/OI Capture | Backend | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-004-episode-vol-oi` | REARCH-001, REARCH-003, REARCH-002 |
| 6 | [REARCH-006](https://github.com/bhaveshhpatel/cipher/issues/95) | WSJ Steamroom Signaling Engine — Vol/OI as Composite Factor | Backend | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-006-steamroom-signal-engine` | REARCH-004 |
| 7 | [REARCH-007](https://github.com/bhaveshhpatel/cipher/issues/96) | Gate Config Store — Seed New Keys | DB Migration | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-007-gate-config-seeds` | REARCH-001, REARCH-006 |
| 8 | [REARCH-008](https://github.com/bhaveshhpatel/cipher/issues/97) | Chain Store Admin API — Status + Manual Refresh | Backend | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-008-chain-store-admin-api` | REARCH-001 |
| 9 | [REARCH-009](https://github.com/bhaveshhpatel/cipher/issues/98) | Ingestion Gate Admin API — Full Parameter Surface | Backend | 🔲 Not Started | 3-way (SA+PBE+QA) | `feature/rearch-009-ingestion-gate-api` | REARCH-007 |
| 10 | [REARCH-010](https://github.com/bhaveshhpatel/cipher/issues/99) | Admin UI — Gate Control Panel (Knob Page) | Frontend | 🔲 Not Started | 5-way (SA+PUX+PFE+PBE+QA) | `feature/rearch-010-admin-gate-ui` | REARCH-009, REARCH-008 |
| 11 | [REARCH-011](https://github.com/bhaveshhpatel/cipher/issues/100) | Signal Card UI — Vol/OI Ratio Badge + Score Breakdown | Frontend | 🔲 Not Started | 5-way (SA+PUX+PFE+PBE+QA) | `feature/rearch-011-signal-card-vol-oi` | REARCH-006, REARCH-004 |
| 12 | [REARCH-012](https://github.com/bhaveshhpatel/cipher/issues/101) | Integration + Regression Test Suite | Testing | 🔲 Not Started | 3-way (SA+PBE+QA) | (on `cipher-rearch` directly) | All REARCH-001–011 |

---

## Dependency Graph

```
REARCH-003 (schema)
    └──► REARCH-002 (event enrichment)
    └──► REARCH-004 (episode enrichment)

REARCH-001 (chain store)
    └──► REARCH-002 (event enrichment)
    └──► REARCH-004 (episode enrichment)
    └──► REARCH-005 (index exclusion hardening)
    └──► REARCH-007 (gate config seeds) ←── REARCH-006 (signal engine)
    └──► REARCH-008 (chain store admin api)

REARCH-004 ──► REARCH-006 (signal engine) ──► REARCH-011 (signal card UI)

REARCH-007 ──► REARCH-009 (gate admin api) ──► REARCH-010 (admin gate UI)
REARCH-008 ─────────────────────────────────► REARCH-010 (admin gate UI)

All above ──► REARCH-012 (integration tests) ──► PR: cipher-rearch → main
```

---

## Parallel Tracks

These pairs can be worked simultaneously:

- **Track A (Data Foundation):** REARCH-003 + REARCH-001 in parallel (no cross-dependency)
- **Track B (Enrichment):** REARCH-002 + REARCH-005 in parallel (both depend on 001+003)
- **Track C (Signal + Admin):** REARCH-006 + REARCH-008 in parallel after 001+003+004 done
- **Track D (Frontend):** REARCH-010 + REARCH-011 in parallel after their respective backend deps

---

## Branching Protocol

```
main
 └── cipher-rearch  (aggregation — DO NOT push directly)
      ├── feature/rearch-001-chain-store
      ├── feature/rearch-002-event-enrichment
      ├── feature/rearch-003-schema-migration
      ├── feature/rearch-004-episode-vol-oi
      ├── feature/rearch-005-index-exclusion
      ├── feature/rearch-006-steamroom-signal-engine
      ├── feature/rearch-007-gate-config-seeds
      ├── feature/rearch-008-chain-store-admin-api
      ├── feature/rearch-009-ingestion-gate-api
      ├── feature/rearch-010-admin-gate-ui
      └── feature/rearch-011-signal-card-vol-oi
```

**Rules:**
1. Each feature branch cuts from `cipher-rearch` (not `main` or `admin`)
2. PRs target `cipher-rearch` — never `main` or `admin`
3. `cipher-rearch` → `main` PR opens only after REARCH-012 closes with full QA sign-off
4. No squash merges into `cipher-rearch` — preserve commit history for audit

---

## Deliberation Roles

| Role | Description |
|---|---|
| **SA** | System Architect — owns cross-cutting design decisions, API contracts, performance constraints |
| **PBE** | Principal Backend Engineer — owns implementation detail, DB patterns, async safety |
| **PUX** | Principal UX Designer — owns interaction design, component layout, information hierarchy |
| **PFE** | Principal Frontend Engineer — owns component architecture, WebSocket handling, state management |
| **QA** | Quality Assurance — owns test strategy, coverage gates, regression scope |

**Backend-only stories** require **3-way: SA + PBE + QA** before implementation begins.  
**Frontend stories** require **5-way: SA + PUX + PFE + PBE + QA** before implementation begins.

---

## WSJ Steamroom Signal Defaults (Configurable)

All values below are seeded into `gate_configs` via REARCH-007 and exposed in the admin UI via REARCH-010. None require code changes to adjust.

### Ingestion Gates
| Parameter | T1 Default | T2 Default | T3 Default |
|---|---|---|---|
| `min_premium` | $25,000 | $10,000 | $5,000 |
| `dedup_window_ms` | 500ms | 2,000ms | 5,000ms |
| `signal_debounce_ms` | 30,000ms | — | — |
| `signal_min_premium` | $75,000 | — | — |

### Signal Weights
| Factor | Default Weight | Vol/OI Threshold |
|---|---|---|
| Flow score | 0.40 | — |
| Premium tier | 0.20 | — |
| **Vol/OI ratio** | **0.20** | ≥1.0 → 1.0, ≥0.5 → 0.65, ≥0.2 → 0.35, NULL → 0.10 |
| Backtest | 0.10 | — |
| Multi-day repeat | 0.10 | — |

---

## Index Exclusion (Permanent)

The following symbols are excluded at **two levels** (stream gate + registry build). This is not configurable — Cipher tracks single-stock flow only.

```
Broad ETFs:     SPY, QQQ, IWM, DIA
Volatility:     VXX, UVXY, SVXY
Commodity/Bond: GLD, SLV, TLT, HYG, EEM
Leveraged:      TQQQ, SOXL, SOXS, TECS, TECL
ARK:            ARKK, ARKQ, ARKW, ARKG, ARKX
Sector:         XLF, XLE, XLK, XBI, IBB, IBIT, GDX, GDXJ
```

The `exclude_indices` toggle in `gate_config_store` controls the **stream-time filter only** (for operational flexibility). Registry-level and watchlist-level exclusion is unconditional.

---

## Status Key

| Icon | Meaning |
|---|---|
| 🔲 | Not Started |
| 🔵 | In Deliberation |
| 🟡 | In Progress |
| 🟢 | Merged to `cipher-rearch` |
| ✅ | QA Signed Off |
