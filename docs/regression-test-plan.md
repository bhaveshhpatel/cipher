# Cipher — Regression & Test Plan

> Last updated: 2026-04-23

---

## Automated Test Coverage

### Auth
| Test | File | Status |
|------|------|--------|
| POST /auth/register — success 201 | `tests/test_auth.py` | ✅ |
| POST /auth/login — success 200 + JWT | `tests/test_auth.py` | ✅ |
| GET /auth/me — authenticated | `tests/test_auth.py` | ✅ |
| GET /auth/me — unauthenticated 401 | `tests/test_auth.py` | ✅ |

### Tradier Stream — Failure Mode Regression (F1–F9)
| Test ID | Failure Mode | Test Name | File |
|---------|-------------|-----------|------|
| F1 | Token re-fetched on every reconnect | `test_f1_token_fetched_per_reconnect` | `tests/test_tradier_stream.py` |
| F2 | 401 on stream does NOT permanently fall to demo | `test_f2_stream_401_does_not_permanently_fall_to_demo` | `tests/test_tradier_stream.py` |
| F3 | Idle watchdog triggers on dead connection | `test_f7_watchdog_raises_on_idle` | `tests/test_tradier_stream.py` |
| F4 | Backoff increases with attempt, cap respected | `test_increases_with_attempt`, `test_cap_respected` | `tests/test_tradier_stream.py` |
| F4 | Backoff has jitter (non-deterministic) | `test_jitter_non_deterministic` | `tests/test_tradier_stream.py` |
| F4 | Backoff base case (attempt=0) | `test_base_case_zero_attempt` | `tests/test_tradier_stream.py` |
| F5 | Session fetch retried 3x on timeout | `test_f5_retries_on_timeout` | `tests/test_tradier_stream.py` |
| F5 | Session fetch recovers on second attempt | `test_f5_succeeds_on_second_attempt` | `tests/test_tradier_stream.py` |
| F6a | Session 401 returns None (bad API key) | `test_returns_none_on_401` | `tests/test_tradier_stream.py` |
| F6a | Session missing sessionid returns None | `test_returns_none_on_missing_sessionid` | `tests/test_tradier_stream.py` |
| F7 | Watchdog passes when lines arrive in time | `test_f7_watchdog_passes_on_active_stream` | `tests/test_tradier_stream.py` |
| F8 | Demo mode task cancels cleanly | `test_f8_demo_mode_cancels_cleanly` | `tests/test_tradier_stream.py` |
| F8 | Demo mode emits valid signals | `test_f8_demo_mode_emits_signals` | `tests/test_tradier_stream.py` |
| F9 | Stats dict has all required keys + mode field | `test_get_stats_returns_dict`, `test_mode_field_exists` | `tests/test_tradier_stream.py` |

### Tradier Stream — Market-Hours Guard & Backoff Fix (commit 9a32d4b)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| MH-1 | `_is_market_hours()` returns False on Saturday | `test_market_hours_saturday_is_false` | `tests/test_tradier_stream.py` |
| MH-2 | `_is_market_hours()` returns False on Sunday | `test_market_hours_sunday_is_false` | `tests/test_tradier_stream.py` |
| MH-3 | `_is_market_hours()` returns False before 09:30 ET weekday | `test_market_hours_before_open_is_false` | `tests/test_tradier_stream.py` |
| MH-4 | `_is_market_hours()` returns False after 16:00 ET weekday | `test_market_hours_after_close_is_false` | `tests/test_tradier_stream.py` |
| MH-5 | `_is_market_hours()` returns True at 10:00 ET Monday | `test_market_hours_open_weekday_is_true` | `tests/test_tradier_stream.py` |
| MH-6 | Loop sleeps 60s and logs when market closed | `test_loop_sleeps_when_market_closed` | `tests/test_tradier_stream.py` |
| MH-7 | Mode set to `market_closed` when outside hours | `test_mode_is_market_closed_outside_hours` | `tests/test_tradier_stream.py` |
| BF-1 | `reconnect_attempt` resets to 0 when `session_ticks > 0` | `test_backoff_resets_when_ticks_received` | `tests/test_tradier_stream.py` |
| BF-2 | `reconnect_attempt` increments when `session_ticks == 0` | `test_backoff_increments_when_no_ticks` | `tests/test_tradier_stream.py` |
| BF-3 | Backoff reaches ~60s cap after 4+ zero-tick connections | `test_backoff_reaches_cap_after_zero_tick_closes` | `tests/test_tradier_stream.py` |

### Options Universe — symbols_loader.py
| Test ID | Scenario | File |
|---------|----------|------|
| SL-1 | Happy path — returns validated symbols list | `tests/test_symbols_loader.py` |
| SL-2 | Tradier 401 — returns seed fallback | `tests/test_symbols_loader.py` |
| SL-3 | Network error — returns seed fallback | `tests/test_symbols_loader.py` |
| SL-4 | Empty results from Tradier — returns seed fallback | `tests/test_symbols_loader.py` |
| SL-5 | Single-dict Tradier response (not list) — handled correctly | `tests/test_symbols_loader.py` |
| SL-6 | Lowercase symbols normalized to uppercase | `tests/test_symbols_loader.py` |
| SL-7 | Exception on single symbol does not abort whole batch | `tests/test_symbols_loader.py` |
| SL-8–20 | All 6 `load_universe()` fallback scenario branches | `tests/test_symbols_loader.py` |

### Options Universe — universe_store.py
| Test ID | Scenario | File |
|---------|----------|------|
| US-1 | `load_fresh_snapshot` — snapshot < 24h old returned | `tests/test_universe_store.py` |
| US-2 | `load_fresh_snapshot` — no rows → returns None | `tests/test_universe_store.py` |
| US-3 | `load_any_snapshot` — stale snapshot returned as fallback | `tests/test_universe_store.py` |
| US-4 | `save_snapshot` — empty symbol list rejected | `tests/test_universe_store.py` |
| US-5 | `save_snapshot` — insert failure handled | `tests/test_universe_store.py` |
| US-6 | DB exception propagates correctly | `tests/test_universe_store.py` |
| US-7 | Prunes to last 7 snapshots on save | `tests/test_universe_store.py` |
| US-8 | Batch insert fires for > 500 symbols | `tests/test_universe_store.py` |
| US-9 | `snapshot_id` generated via `uuid4()` in Python — passed in payload | `tests/test_universe_store.py` |
| US-10 | No `.select()` chained after `.insert()` (supabase-py v2 guard) | `tests/test_universe_store.py` |

### Flow Store — DB Signal Persistence (commit 701aaf6)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| FS-1 | `flow_episode` row matches `flow_episodes` schema; no old `composite_signals` columns | `test_flow_episode_row_schema` | `tests/test_flow_store.py` |
| FS-2 | `flow_events` row has no `id` field (Postgres generates uuid) | `test_flow_event_row_no_id` | `tests/test_flow_store.py` |
| FS-3 | Sparse/None input produces safe defaults for all nullable fields | `test_flow_event_sparse_defaults` | `tests/test_flow_store.py` |
| FS-4 | f-string log with None/zero values does not raise | `test_fstring_log_none_fields_no_crash` | `tests/test_flow_store.py` |
| FS-5 | Buffer accumulates rows and drains atomically | `test_buffer_accumulate_and_drain` | `tests/test_flow_store.py` |
| FS-6 | `start_flow_writer` no-ops when SUPABASE_URL/KEY not set | `test_no_op_without_supabase_env` | `tests/test_flow_store.py` |
| FS-7 | `persist_flow_episode` calls `_insert_rows("flow_episodes", ...)` not `composite_signals` | `test_persist_flow_episode_calls_correct_table` | `tests/test_flow_store.py` |
| FS-8 | `persist_flow_event` buffers without any network call | `test_persist_flow_event_buffers_without_network` | `tests/test_flow_store.py` |

### Resolved Regressions — flow_store.py
| ID | Bug | Symptom | Root Cause | Fix (commit 701aaf6) |
|----|-----|---------|------------|----------------------|
| REG-FS-1 | Wrong DB table | 400 on every signal persist | `flow_store.py` posting to `composite_signals` (wrong schema) | Retargeted to `flow_episodes`; renamed function `persist_flow_episode` |
| REG-FS-2 | Client-sent `id` field | 400 on `flow_events` and `flow_episodes` inserts | `id` included in row dict; Postgres uuid/bigserial column rejects client-provided value | Removed `id` from both `_make_event_row` and `_make_episode_row` |
| REG-FS-3 | Log crash on None fields | `TypeError` in logging thread when signal fields are None | `log.info("... %,.0f", None)` — `%`-style defers eval to formatter, crashes on None | Switched all log calls to f-strings (evaluated immediately; None renders as "None") |

### Signal Pipeline
| Test | File | Status |
|------|------|--------|
| Flow scan response validation | `tests/test_flow.py` | ✅ |
| Stream stats response validation | `tests/test_stream.py` | ✅ |
| Simulation validation guardrails | `tests/test_simulation.py` | ✅ |
| WebSocket real-time signal receipt | `tests/test_ws.py` | ✅ |

---

## Phase 3 — Smart Signals, WS Heartbeat, Parser Guard, Volume-Weighted Score

### Parser — Size Field Guard (options_flow_parser.py)
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-P-1 | `raw` dict missing `size` key entirely | Returns `None` (no event emitted) |
| P3-P-2 | `raw["size"]` is `None` | Returns `None` |
| P3-P-3 | `raw["size"]` is `0` | Returns `None` |
| P3-P-4 | `raw["size"]` is `"0"` (string zero) | Returns `None` |
| P3-P-5 | `raw["size"]` is valid positive int | Returns valid `OptionsFlowEvent` |
| P3-P-6 | `raw["size"]` is valid positive string int | Returns valid `OptionsFlowEvent` |

### Signal Engine — Volume-Weighted Premium Factor
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-S-1 | `open_interest == 0` on latest event | `volume_weighted_premium_factor()` returns `0.5` |
| P3-S-2 | `open_interest` unavailable (field absent) | Returns `0.5` neutral |
| P3-S-3 | `total_premium / (OI × 100)` < 1.0 | Returns ratio rounded to 3dp |
| P3-S-4 | Very high premium vs low OI — ratio > 1.0 | Capped at `1.0` |
| P3-S-5 | Composite score uses weights `0.55/0.35/0.10` | `comp == flow*0.55 + bt*0.35 + vwp*0.10` |
| P3-S-6 | `CompositeSignal` includes `volume_premium_factor` field | Field present and 0–1 range |
| P3-S-7 | Reasoning string includes volume-premium factor | String contains `volume-premium factor` |

### Smart Signals Router — Pagination & Filters
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-R-1 | `GET /api/signals/list` — no params | 200, returns 20 results, page=1 |
| P3-R-2 | `GET /api/signals/list?page=2&page_size=5` | 200, returns up to 5 results, page=2 |
| P3-R-3 | `page_size=101` | 422 Unprocessable Entity |
| P3-R-4 | `page=0` | 422 Unprocessable Entity |
| P3-R-5 | `direction=bullish` | Returns only signals with `recommendation=="BUY"` |
| P3-R-6 | `direction=bearish` | Returns only signals with `recommendation=="SELL"` |
| P3-R-7 | `direction=neutral` | Returns only signals with `recommendation=="HOLD"` |
| P3-R-8 | `direction=invalid` | 422 with valid values listed |
| P3-R-9 | `tier=invalid` | 422 with valid values listed |
| P3-R-10 | `min_conviction=0.65` | Returns only signals where `composite_score >= 0.65` |
| P3-R-11 | `min_conviction=1.1` | 422 (exceeds max 1.0) |
| P3-R-12 | `min_conviction=-0.1` | 422 (below min 0.0) |
| P3-R-13 | Unauthenticated request | 401 |
| P3-R-14 | `CompositeOut` response includes `volume_premium_factor` | Field present |
| P3-R-15 | `total` in response reflects filtered count, not full count | Correct filtered total |

### WebSocket Heartbeat (ws.py)
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-W-1 | Client connects, server sends `{"type":"ping"}` within 25s | Ping received |
| P3-W-2 | Client responds `{"type":"pong"}` within 10s | Connection stays open |
| P3-W-3 | Client does NOT respond to ping within 10s | Server closes with code 1001 |
| P3-W-4 | Client sends non-pong message in ping window | Server logs warning, connection may continue |
| P3-W-5 | `stop_event` set externally | Heartbeat task exits cleanly |
| P3-W-6 | WebSocket disconnect during heartbeat | `stop_event` set, task cancelled, bus unsubscribed |
| P3-W-7 | Invalid JWT on connect | Closed with code 4001, no heartbeat started |

### Manual Regression — Phase 3
- [ ] `GET /api/signals/list` returns paginated JSON with `signals`, `page`, `page_size`, `total`
- [ ] `?direction=bullish&min_conviction=0.65` filters correctly — all returned signals are BUY ≥ 0.65
- [ ] `?page=2&page_size=5` returns correct slice of results
- [ ] `GET /api/signals/composite/AAPL` response includes `volume_premium_factor` field
- [ ] Composite scores use new `0.55/0.35/0.10` weights (verify via reasoning string)
- [ ] Browser WS devtools: ping frame arrives every ~25s
- [ ] Browser WS devtools: pong reply sent by frontend keeps connection alive
- [ ] Railway logs: `WS pong timeout — closing connection` does NOT appear during normal operation
- [ ] Parser: flow events with missing/null/zero `size` do not reach accumulator (verify via Railway logs — no `prem=$0` entries)

---

## Running Tests

```bash
# All tests
cd backend
pip install -r requirements-dev.txt
pytest tests/ -v

# Flow store tests only (DB persistence regression)
pytest tests/test_flow_store.py -v

# Tradier stream failure modes only
pytest tests/test_tradier_stream.py -v

# Market-hours guard tests only
pytest tests/test_tradier_stream.py -k "market_hours or backoff" -v

# Universe tests only
pytest tests/test_symbols_loader.py tests/test_universe_store.py -v

# Live Tradier integration test (requires real key, skip in CI)
TRADIER_API_KEY=<your_key> pytest tests/test_tradier_stream.py \
    -k test_live_tradier_session_token -v
```

Equivalent manual curl for session token:
```bash
curl -X POST https://api.tradier.com/v1/markets/events/session \
     -H 'Authorization: Bearer <your_key>' \
     -H 'Accept: application/json' \
     -d ''
```

---

## Manual Regression Checklist

### Auth
- [ ] Register new account → 201, JWT returned
- [ ] Login → 200, JWT stored in localStorage
- [ ] Dashboard redirect when unauthenticated → `/login`
- [ ] `/me` with valid JWT → user object returned

### Dashboard
- [ ] Flow tab fetches and renders signal cards
- [ ] Swarm tab runs simulation and renders verdict
- [ ] Signal feed updates live via WebSocket
- [ ] Stream stats bar updates (ticks, signals, mode)
- [ ] Composite card loads correctly
- [ ] Frontend uses `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL`

### Tradier Stream (Railway logs)
- [ ] On deploy: logs show `Tradier session token obtained successfully`
- [ ] Logs show `Tradier stream connected — monitoring N symbols`
- [ ] After 5 min: logs show reconnect, NOT `401 — Falling back to demo mode`
- [ ] Mode in `/health` or stats endpoint shows `live` not `demo`
- [ ] **Outside market hours:** logs show `Market closed (ET: ...) — sleeping 60s` once per minute
- [ ] **Outside market hours:** `/health` mode shows `market_closed`
- [ ] **At market open (09:30 ET):** stream transitions from `market_closed` → `live` automatically

### DB Signal Persistence (flow_store.py) — commit 701aaf6
- [ ] Railway logs show `[flow_store] DB writer subscribed to bus — flow_episodes will be persisted`
- [ ] Railway logs show `[flow_store] flow_episode saved: <ticker> <contract_type> alert=<level> prem=$<amount>` (no crash, no None format error)
- [ ] Supabase `flow_episodes` table receives rows within seconds of signals qualifying
- [ ] Supabase `flow_events` table receives batched rows every ~5 seconds during market hours
- [ ] No rows appear in `composite_signals` table (old, wrong target — should be empty/absent)
- [ ] `flow_episodes.id` is auto-generated by Postgres (bigserial), not client-sent
- [ ] `flow_events.id` is auto-generated by Postgres (uuid), not client-sent
- [ ] Railway logs show `[flow_store] flushed N flow_events to DB` on each flush cycle

---

## Performance Scenarios (Backlog)
- Load test `/api/flow/scan` with 50 concurrent authenticated users
- WebSocket fan-out with 50+ concurrent subscribers
- Stream processor benchmark under 1k ticks/minute
- Frontend render profiling for 200-signal feed cap
- `flow_events` batch write latency under high tick volume (1k+/min)
