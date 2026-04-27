# Cipher Apex — Branch, Deployment & Database Strategy

> Generated: 2026-04-27

---

## The Core Tension You Need to Resolve First

You said: **"access production DB in Supabase as readonly."**

That immediately creates a problem with Phases 2, 4B, and 5:

- **Phase 2** (Stacking Accumulator) writes `flow_events` and `flow_episodes` to production DB
- **Phase 4B** needs a new `ticker_levels` table
- **Phase 5** needs an `exit_signals` table or channel

If the apex branch is readonly against production, **the signal engine is broken by design** — it can't persist episodes or fire exit signals. You can't just observe production data and call it a working test.

The real choice is one of these three:

| Option | What It Means | Tradeoff |
|--------|--------------|----------|
| **A — Parallel schema** | New tables (`apex_flow_events`, `apex_flow_episodes`, `apex_signals`) on production. Apex branch writes to apex tables, reads from production tables for context | ✅ Real data, real writes, no prod contamination. Sunset old tables when Apex is proven |
| **B — Staging Supabase project** | Spin up a second free Supabase project, seed it with a prod snapshot, apex branch writes there | ✅ Full isolation but stale data. Fine for unit behavior, bad for live signal testing |
| **C — Feature-flagged prod writes** | Apex branch deploys to prod DB but all writes go behind `APEX_MODE=true` env var. Old pipeline and new pipeline run in parallel, old tables still active | ✅ Live data + real writes, highest risk if a bug slips through |

**Option A is the right call here.** Parallel schema on production, apex branch has full read+write to `apex_*` tables only.

---

## Proposed Branch + Deployment Strategy

### Git Branching Model

```
main                    ← production (Railway + Vercel prod)
  └── apex/engine       ← all Apex work lives here
        ├── apex/phase-1-hard-gates
        ├── apex/phase-2-accumulator
        ├── apex/phase-3-scoring
        ├── apex/phase-4a-sector-sympathy
        ├── apex/phase-4b-sr-levels
        ├── apex/phase-5-exit-engine
        └── apex/phase-6-swarm-tiering
```

Each phase gets its own branch off `apex/engine`, not off `main`. PRs merge into `apex/engine`, never directly to `main`. When all phases are proven, one final PR: `apex/engine → main`.

---

### Vercel Preview Setup

Vercel already auto-deploys preview URLs per branch — no extra config needed. When you push `apex/engine`, Vercel gives you a preview URL (e.g. `cipher-apex-engine.vercel.app`). That preview deployment gets its own env vars pointing to:

```
SUPABASE_URL         = <production URL>         # same as prod
SUPABASE_ANON_KEY    = <production anon key>    # readonly reads fine
SUPABASE_SERVICE_KEY = <production service key> # needed for apex_* writes
APEX_MODE            = true
RAILWAY_APEX_URL     = <apex railway service>
```

Frontend on the apex preview reads from `apex_signal_history`, `apex_flow_events` etc. — completely decoupled from what the prod frontend is showing users.

---

### Railway Apex Service

Don't redeploy the existing Railway service. **Add a second Railway service** in the same project:

```
cipher-backend          ← existing, untouched, running main branch
cipher-backend-apex     ← new service, tracking apex/engine branch
```

Both services connect to the same Supabase project. `cipher-backend` writes to `flow_events`, `flow_episodes`, `signal_history`. `cipher-backend-apex` writes to `apex_flow_events`, `apex_flow_episodes`, `apex_signal_history`. They can both run the Tradier stream simultaneously — dedup at the DB level is per-table anyway.

---

### Database Schema Strategy (Option A in detail)

#### Phase 1–3 (no new tables needed yet)

Hard gates and scoring changes are in-memory — nothing new written to DB at this phase. The apex service uses existing `flow_events` table in **readonly** mode to validate that hard gate rejection is working correctly. You can add an `apex_hard_rejected` counter table (1 row, running total) for observability if you want.

#### Phase 4B rollout (first new table)

```sql
-- Migration: 001_apex_ticker_levels.sql
CREATE TABLE apex_ticker_levels (
  ticker        TEXT PRIMARY KEY,
  support       NUMERIC,
  resistance    NUMERIC,
  atr           NUMERIC,
  updated_at    TIMESTAMPTZ DEFAULT now()
);
```

No sunset of old tables at this point — purely additive.

#### Phase 5 rollout (exit signals table)

```sql
-- Migration: 002_apex_exit_signals.sql
CREATE TABLE apex_exit_signals (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  episode_id    UUID REFERENCES apex_flow_episodes(id),
  signal_type   TEXT,  -- 'partial_profit' | 'steam_dry' | 'counter_flow' | 'level_break' | 'time_stop'
  ticker        TEXT,
  triggered_at  TIMESTAMPTZ DEFAULT now(),
  payload       JSONB
);
```

#### When Apex is proven → sunset plan

```
Week 1 after merge:   apex_* tables are primary, old tables frozen (no writes)
Week 2:               Frontend fully switched to apex_* tables
Week 3:               Old tables archived (pg_dump), then dropped
```

---

### Frontend Phasing

The frontend needs updates at **3 specific phase boundaries**, not after every phase:

| After Phase | Frontend Change | Scope |
|-------------|----------------|-------|
| Phase 1–3 complete | Add `APEX_MODE` toggle in UI — show apex signals alongside current signals for comparison. No new components yet | Small — env-driven conditional |
| Phase 4B + 5 complete | New `ExitSignalFeed` component on signal detail view. New `apex_exit_signals` WebSocket channel | Medium — new component + new WS handler |
| `apex/engine → main` merge | Swap all data sources from old tables to `apex_*` tables. Remove comparison toggle. Remove old signal components | Final cleanup pass |

The key insight: **don't build new frontend components until the backend phase they depend on is stable.** Phase 1–3 backend changes are invisible to the frontend — the signals that fire look identical, just higher quality. So the frontend doesn't need to change at all until Phase 4B/5 introduce genuinely new data types (exit signals, S/R levels).

---

## Recommended Start Sequence

```
1. Create apex/engine branch off main right now
2. Add Railway cipher-backend-apex service tracking apex/engine
3. Set APEX_MODE=true + apex_* table env vars on that service
4. Run migration 000 — create apex_flow_events, apex_flow_episodes,
   apex_signal_history as clones of existing schema (no data)
5. Start Phase 1 (signal_gate.py) on apex/phase-1-hard-gates
```

Steps 1–4 are pure infrastructure with zero code changes — they can be done in 30 minutes and give you a safe sandbox before a single line of Apex logic is written.
