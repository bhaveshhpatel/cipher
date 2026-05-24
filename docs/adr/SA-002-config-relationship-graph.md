# ADR SA-002 — Config Relationship Graph: signal_config ↔ gate_configs ↔ debounce_config ↔ excluded_symbols_config

**Status:** Accepted  
**Date:** 2026-05-24  
**Author:** Architecture Review  
**Ticket:** SA-2

---

## Context

Cipher's runtime configuration is split across two tables and two stores.
Operators and engineers have repeatedly confused which knob lives where,
leading to:

- Live-tune attempts on `signal_config` that silently no-op because the
  threshold being tuned is actually enforced at the ingestion (gate) layer.
- Phantom "debounce_config" and "excluded_symbols_config" concepts that do
  not correspond to independent tables, modules, or stores — they are logical
  labels for specific keys inside `gate_configs`.
- Incorrect assumption that `gate_configs` and `signal_config` are evaluated
  in parallel; they are sequential stages.

This ADR defines the authoritative relationship graph, documents invariants
that code must not violate, and calls out the one known overlap that requires
a follow-up decision.

---

## Decision

### 1. The Two Stores Are Sequential, Not Parallel

```
Tradier WebSocket
       │
       ▼
 OCC Parser / Accumulator
       │
       ▼
 ┌─────────────────────────────────────────┐
 │          gate_configs  (GateConfigStore) │  ← STAGE 1: ingestion-time gates
 │  min_premium        (by tier)            │
 │  dte_floor_multiplier (by tier)          │
 │  dedup_window_ms    (by tier)            │
 │  require_oi         (by tier)            │
 │  signal_debounce_ms (by tier) ← debounce │
 │  signal_min_premium (by tier)            │
 │  exclude_indices    (by tier) ← excluded │
 └─────────────────────────────────────────┘
       │  episode formed only if ALL gate checks pass
       ▼
 flow_episodes table
       │
       ▼
 ┌─────────────────────────────────────────┐
 │       signal_config  (SignalConfigStore) │  ← STAGE 2: signal-emission gates
 │  sig.golden_sweep_premium  (T1 base)    │
 │  sig.block_premium         (T1 base)    │
 │  sig.noteworthy_premium    (T1 base)    │
 │  sig.*_t2_mult / *_t3_mult  (PBE mults) │
 │  sig.require_ask_side                   │
 │  sig.ask_side_pct_floor                 │
 │  sig.require_vol_gt_oi                  │
 │  sig.min_dte / sig.max_dte              │
 │  sig.min_trade_count                    │
 │  sig.steamroom_score_floor              │
 └─────────────────────────────────────────┘
       │  signal emitted only if ALL signal checks pass
       ▼
 Alert / Notification Pipeline
```

A flow trade that fails any `gate_configs` check never reaches
`signal_config`. These stages **cannot** be reordered or merged.

---

### 2. "debounce_config" Is Not a Separate Entity

There is no `debounce_config` table, module, class, or store.

**Canonical location:** `gate_configs.signal_debounce_ms` (per-tier float, ms).

**Alias:** `GateConfigStore._ALIAS_MAP` maps `"debounce_ms"` →
`"signal_debounce_ms"` at read time. All call-sites MUST use the canonical
name `signal_debounce_ms` in code and admin API payloads. The alias exists
only as a legacy compatibility shim for early integrations.

**Defaults (from `_DEFAULTS` / migration 021 seed):**

| Tier | signal_debounce_ms |
|------|--------------------|
| T1   | 30 000 ms (30 s)   |
| T2   | 60 000 ms (60 s)   |
| T3   | 120 000 ms (120 s) |

**Admin write surface:** `PATCH /admin/gate-config` with body
`{"gate": "signal_debounce_ms", "tier": <1|2|3>, "value": <ms>}`.
Writing to `PATCH /admin/signal-config` for debounce is a bug — it targets
the wrong store and will silently no-op.

---

### 3. "excluded_symbols_config" Is Not a Separate Entity

There is no `excluded_symbols_config` table, module, class, or store.

**Canonical location:** `gate_configs.exclude_indices` (per-tier float,
boolean-as-float: `1.0` = exclude index-like symbols, `0.0` = allow).

**Defaults (from `_DEFAULTS` / migration 021 seed):**

| Tier | exclude_indices |
|------|------------------|
| T1   | 1.0 (exclude)    |
| T2   | 1.0 (exclude)    |
| T3   | 1.0 (exclude)    |

**Future scope:** A per-symbol exclusion list (e.g., "never alert on SPY,
QQQ regardless of tier") is a **future feature** requiring a separate
`excluded_symbols` table, not an extension of `exclude_indices`. Do not
add per-symbol logic to the existing boolean gate.

**Admin write surface:** `PATCH /admin/gate-config` with body
`{"gate": "exclude_indices", "tier": <1|2|3>, "value": <0|1>}`.

---

### 4. Cache TTL Invariants

| Store             | TTL    | Refresh mechanism                         | Fallback on DB unreachable |
|-------------------|--------|-------------------------------------------|----------------------------|
| GateConfigStore   | 300 s  | `start_refresh_loop(interval_s=300)`      | `_DEFAULTS` hardcodes      |
| SignalConfigStore | 30 s   | `_maybe_refresh()` on every `get_param()` | `_DEFAULTS` hardcodes      |

The 30 s `signal_config` TTL is intentionally shorter than the 300 s
`gate_configs` TTL. Signal thresholds drive alert emission — operators
expect faster propagation after a live-tune. Gate configs are ingestion
infrastructure and change rarely.

**Invariant:** No caller may bypass TTL by directly mutating `_snapshot` or
`_cache`. All writes go through `store.update()` (gate) or
`update_signal_config()` + `async_reload_signal_config()` (signal).

---

### 5. The One Known Overlap — Premium Threshold Duplication

Both stores carry a premium threshold concept:

| Store         | Key                   | Scope         | Stage enforced |
|---------------|-----------------------|---------------|----------------|
| gate_configs  | `signal_min_premium`  | Per-tier flat | Ingestion       |
| signal_config | `sig.*_premium` + PBE multipliers | Per-tier (base × mult) | Signal emission |

**These are NOT the same gate.** `signal_min_premium` in `gate_configs` is a
blunt ingestion filter that drops episodes before they are stored.
`sig.*_premium` in `signal_config` is the refined signal-emission threshold
that classifies surviving episodes into GOLDEN / BLOCK / NOTEWORTHY alert
levels with tier-aware PBE scaling.

**Current defaults create a logical ordering:**

```
gate_configs.signal_min_premium  T1=75k, T2=50k, T3=25k   (hard floor)
signal_config.sig.noteworthy_premium  T1=50k → T3=10k       (after PBE)
```

T3 episodes between $10k–$25k premium survive the ingestion gate but will
never emit a signal. This is intentional — the gate floor is conservative;
signal config does the fine-grained classification.

**Risk:** Raising `signal_min_premium` in `gate_configs` without raising
`sig.*_premium` floors silently orphans episodes in the DB that will never
emit. Conversely, lowering `sig.*_premium` below `signal_min_premium` is
a no-op — episodes at those premiums were never stored.

**Decision:** Do NOT merge these two thresholds into one config surface.
They serve different pipeline stages. A follow-up ADR (SA-003) must
document the correct operator workflow for tuning premium thresholds across
both stores before any admin UI exposes a single "premium threshold" slider.

---

### 6. Admin Write Surface Summary

| Intent                        | Endpoint                    | Payload keys                          |
|-------------------------------|-----------------------------|---------------------------------------|
| Tune ingestion gate           | `PATCH /admin/gate-config`  | `gate`, `tier`, `value`               |
| Tune debounce (canonical)     | `PATCH /admin/gate-config`  | `gate=signal_debounce_ms`, `tier`, `value` |
| Tune index exclusion          | `PATCH /admin/gate-config`  | `gate=exclude_indices`, `tier`, `value` |
| Tune signal threshold         | `PATCH /admin/signal-config`| `key`, `value`                        |
| Force signal config reload    | `POST /admin/signal-config/reload` | —                             |

**Cross-write is a bug.** Writing signal keys to `/admin/gate-config` or
gate keys to `/admin/signal-config` will either be rejected by validation
or silently no-op. The admin API must enforce this at the route layer.

---

## Consequences

### Positive
- Eliminates phantom "debounce_config" and "excluded_symbols_config"
  abstractions that do not exist in code.
- Clarifies sequential evaluation order so operators know which store to
  tune for ingestion-vs-emission effects.
- Documents the premium threshold overlap and its intentional design before
  it causes a production misconfig.

### Negative / Open
- SA-003 (premium threshold operator guide) is now a blocking dependency
  before any admin UI that presents a unified premium knob.
- The `debounce_ms` alias in `_ALIAS_MAP` should be removed in a future
  cleanup once all call-sites use `signal_debounce_ms`.

### Neutral
- No code changes required to implement this ADR — it is a documentation
  decision. The invariants described here MUST be verified against future
  PRs via code review.

---

## References

- `backend/services/gate_config_store.py` — `_VALID_GATES`, `_DEFAULTS`,
  `_ALIAS_MAP`, `_BOUNDS`, `GateConfigStore`
- `backend/services/signal_config_store.py` — `SIGNAL_CONFIG_TYPES`,
  `_DEFAULTS`, `_TIER_MULT_KEYS`, `SignalConfigStore`
- Migration 021 — `gate_configs` seed (must match `gate_config_store._DEFAULTS`)
- Migration 030/031 — `signal_config` seed (must match `signal_config_store._DEFAULTS`)
- ADR SA-001 — chain refresh and P1-skip worker lifecycle
- ADR SA-003 (pending) — operator guide for tuning premium thresholds across
  both stores
