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

### Signal Pipeline
| Test | File | Status |
|------|------|--------|
| Flow scan response validation | `tests/test_flow.py` | ✅ |
| Stream stats response validation | `tests/test_stream.py` | ✅ |
| Simulation validation guardrails | `tests/test_simulation.py` | ✅ |
| WebSocket real-time signal receipt | `tests/test_ws.py` | ✅ |

---

## Running Tests

```bash
# All tests
cd backend
pip install -r requirements-dev.txt
pytest tests/ -v

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
- [ ] Simulate network drop: logs show reconnect with backoff, not permanent demo
- [ ] **Outside market hours:** logs show `Market closed (ET: ...) — sleeping 60s` once per minute, NOT rapid reconnect spam
- [ ] **Outside market hours:** `/health` mode shows `market_closed`
- [ ] **Outside market hours:** backoff increases across consecutive zero-tick closes (check reconnect_attempt in logs)
- [ ] **At market open (09:30 ET):** stream transitions from `market_closed` → `live` automatically

---

## Performance Scenarios (Backlog)
- Load test `/api/flow/scan` with 50 concurrent authenticated users
- WebSocket fan-out with 50+ concurrent subscribers
- Stream processor benchmark under 1k ticks/minute
- Frontend render profiling for 200-signal feed cap
