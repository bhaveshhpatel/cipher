# Contributing to Cipher

This repository is regression-gated. If your change breaks ingestion, signal persistence, or the
frontend dashboard, it should fail before merge.

---

## Ground Rules

- Source code is the truth. If docs disagree with `backend/` or `frontend/`, update the docs.
- `docs/ARCHITECTURE.md` is the canonical architecture reference. Keep it current when changing the
  6-layer pipeline, startup sequence, bus channels, DB writes, or runtime thresholds.
- Do not merge changes that only update docs while leaving runtime behavior undocumented.
- Do not change thresholds, event payloads, or table writes casually — they propagate across the
  parser, accumulator, persistence layer, dashboards, and tests.

---

## Repo Shape

```
backend/
  main.py                     # lifespan startup, registry ownership, writer startup
  services/
    tradier_stream.py         # live stream entry, _process_trade tick funnel
    stream_manager.py         # shared session token + worker orchestration
    stream_worker.py          # per-worker Tradier POST + telemetry
    symbol_registry.py        # OCC registry + tier map + refresh loop
    flow_store.py             # flow_events + flow_episodes writes
    signal_store.py           # signal_history writes
    universe_store.py         # snapshot idempotency
    chain_store.py            # DB cache for OCC contracts
    tier_engine.py            # T1/T2/T3 assignment
    swarm_engine.py           # explicit AI invocation only
  parsers/
    options_flow_parser.py    # OCC parse + trade classification
  signals/
    repetition_accumulator.py # Gate 1 + Gate 2
    composite_signal_engine.py# composite score
  utils/
    dedup.py                  # DedupCache + sweep detection
  routers/
    ws.py, health.py, admin.py, history.py, ...
frontend/
  src/app, components, hooks, lib/api.ts

docs/
  ARCHITECTURE.md             # source-of-truth architecture file
  SIGNAL_ENGINE.md
  REGRESSION_TESTING.md
```

---

## Local Setup

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
uvicorn main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

---

## Required Environment Variables

### Backend

- `TRADIER_API_KEY`
- `TRADIER_BASE_URL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `JWT_SECRET`

### Optional / feature-scoped

- `SUPABASE_KEY` — anon key for public read paths only
- `GROQ_API_KEY` — required only for SwarmEngine or AI reasoning routes
- `REGISTRY_REFRESH_MINS` — registry refresh override

> Do not use `SUPABASE_KEY` / anon key for backend inserts. `flow_store.py` and `signal_store.py`
> require `SUPABASE_SERVICE_ROLE_KEY` because server-side writes must bypass RLS.

---

## Before You Change Code

Pressure-test the blast radius first. Most bugs here are not local.

### If you change Layer 1 — Symbol Registry

You probably also need to inspect:
- `services/universe_store.py`
- `services/chain_store.py`
- `services/tier_engine.py`
- `main.py` lifespan + prewarm loop
- Snapshot idempotency behavior

### If you change Layer 2 — Stream Ingestion

You probably also need to inspect:
- `services/stream_manager.py`
- `services/stream_worker.py`
- `_process_trade()` in `services/tradier_stream.py`
- `/health/stream` stats
- Worker telemetry logs (`STREAM_STATS`, `STREAM_HEALTH`)

### If you change Layer 3 — Trade Parsing

You probably also need to inspect:
- `parsers/options_flow_parser.py`
- `utils/dedup.py`
- `signals/repetition_accumulator.py`
- `services/flow_store.py`
- Tests covering OCC parse edge cases

### If you change Layer 4 — Deduplication

You probably also need to inspect:
- `utils/dedup.py`
- `_process_trade()` caller semantics
- Sweep retroactive upgrade path in `services/flow_store.py`
- Any tests asserting duplicate vs sweep behavior

### If you change Layer 5 — Repetition / Persistence

You probably also need to inspect:
- `signals/repetition_accumulator.py`
- `services/flow_store.py`
- `services/signal_store.py`
- `routers/history.py`
- Supabase schemas / migrations

### If you change Layer 6 — Signal Engine / Delivery

You probably also need to inspect:
- `signals/composite_signal_engine.py`
- `core/async_bus.py`
- `services/signal_store.py`
- `routers/ws.py`
- Frontend signal consumers

---

## Ingestion Invariants

These are not suggestions. If your change violates one of these, expect downstream breakage.

### Worker / session model

- One shared Tradier session token per stream-manager run.
- Worker batch size is capped at 500 OCC symbols per POST.
- Active worker count is `ceil(registry.size() / 500)`.
- Demo mode is not the automatic fallback path in production.

### Dedup + sweep model

- Dedup key is `(occ_symbol, size, round(fill, 1))`.
- Dedup TTL is 5 seconds.
- Sweep threshold is 3 unique exchanges.
- Sweep upgrades may be retroactive via DB PATCH when threshold is crossed late.

### Accumulator gates

- Gate 1: `trade_count >= 3` OR `total_premium >= 10_000`.
- Gate 2: re-emit only when `total_premium - last_signaled_premium >= 50_000`.
- Alert levels are based on cumulative episode premium, not composite recommendation.

### DB write model

- `flow_events` are buffered: 500ms flush or 100-row early flush.
- `flow_episodes` are written immediately from the `db_writer` bus subscriber.
- `signal_history` is written by `signal_store.py` from the `signal_writer` channel.
- Backend DB writes must use `SUPABASE_SERVICE_ROLE_KEY`.

---

## Documentation Rules

Update docs in the same PR when runtime behavior changes.

### You must update `docs/ARCHITECTURE.md` when changing:

- The 6-layer pipeline
- Startup sequence / lifespan ownership
- Stream worker model
- Dedup logic or thresholds
- Accumulator thresholds or retrigger behavior
- Bus channels or payload shapes
- DB write destinations / field mappings
- Snapshot idempotency behavior

### You should update `README.md` when changing:

- Setup instructions
- Stack summary
- Project structure
- CI/CD behavior
- Environment variables
- High-level architecture summary

### You should update `SIGNAL_ENGINE.md` when changing:

- Composite score formula
- Recommendation thresholds
- Backtest weighting
- Flow-score normalization rules

---

## Testing Rules

Run the narrowest useful test first, then the broader suite.

### Backend

```bash
cd backend
pytest -k test_dedup
pytest -k repetition_accumulator
pytest -k tradier_stream
pytest
```

### Frontend

```bash
cd frontend
npx jest --watch
npx jest --coverage
```

### CI gates

- Backend coverage must stay at or above 90%.
- Frontend global lines/functions must stay at or above 75%.
- `useAuth.ts` remains a special high-coverage file and should not regress.

If you touch ingestion, do not stop at unit tests. You should validate the full event path:
1. Parse
2. Dedup
3. Accumulator Gate 1
4. `persist_flow_event()`
5. Composite signal build
6. Bus publish
7. `flow_episodes` / `signal_history` persistence

---

## Migration Rules

- Use a new numbered migration for schema changes.
- Do not silently repurpose existing columns without updating all writers/readers.
- Do not hardcode generated IDs in data migrations.
- If a schema change affects DB writers, update the writer code and the docs in the same PR.
- If a uniqueness guarantee matters operationally, enforce it in SQL — not just in Python.

---

## Logging Rules

This system is debugged from logs under pressure. Do not make logs worse.

- Preserve high-signal operational logs in ingestion paths.
- If you downgrade a log level, be sure the signal still shows up where production debugging needs it.
- Keep `/health/stream` stats aligned with what `_process_trade()` actually increments.
- If you add a new gate or silent drop path, log it or expose it in stats.

---

## Pull Request Checklist

Before opening a PR, verify all of this:

- [ ] The code change is reflected in docs if behavior changed
- [ ] `docs/ARCHITECTURE.md` is updated if any pipeline/runtime contract changed
- [ ] Relevant backend tests pass locally
- [ ] Relevant frontend tests pass locally
- [ ] New schema changes have a migration
- [ ] New env vars are documented in `README.md`
- [ ] Log/stat changes are consistent with runtime behavior
- [ ] Bus payload shape changes are reflected in all subscribers

---

## Common Failure Modes

These are the recurring mistakes worth avoiding.

- Writing backend rows with the anon Supabase key and then wondering why inserts 42501.
- Changing event payload shape in `_process_trade()` without updating `flow_store.py` or `signal_store.py`.
- Tweaking dedup rules without testing sweep retroactive upgrade behavior.
- Changing accumulator thresholds without updating docs and downstream expectations.
- Treating `recommendation` and `alert_level` as interchangeable fields. They are not.
- Forgetting that `stream_options_flow()` behaves differently in lifespan mode vs standalone mode.
- Updating architecture docs from memory instead of from the source files.

---

## Style Expectations

- Prefer explicitness over cleverness in ingestion code.
- Keep hot-path changes small and measurable.
- If you add a background task, define ownership clearly: lifespan-owned, manager-owned, or worker-owned.
- If you change a threshold, put it behind a named constant or setting.
- If you change a field mapping to the database, document the exact source field.

---

## When in Doubt

Start from source code, not assumptions:
- `services/tradier_stream.py` for the real tick funnel
- `signals/repetition_accumulator.py` for gating truth
- `services/flow_store.py` for DB write truth
- `core/async_bus.py` for fan-out truth
- `docs/ARCHITECTURE.md` only after you have verified the code
