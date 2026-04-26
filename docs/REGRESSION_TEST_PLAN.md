# Regression Test Suite — Coverage Audit & Roadmap
**Date:** April 2026  |  **Codebase:** Cipher (Backend Python/FastAPI + Frontend Next.js/TypeScript)

---

## Current Coverage Summary

| Layer | Modules | Covered | Partial/Stub | No Tests | Coverage % |
|---|---|---|---|---|---|
| Backend | 34 | 13 | 13 | 8 | **55.6%** |
| Frontend | 7 | 2 | 2 | 3 | **42.9%** |
| **Overall** | **41** | **15** | **15** | **11** | **53.4%** |

> **112 gap items** identified across all modules before reaching 100%.

---

## Backend — Module Coverage Detail

### ✅ Fully Covered (no new test files needed)
| Module | Test File | Remaining Gaps |
|---|---|---|
| `core/async_bus.py` | `test_async_bus.py` | Concurrent sub edge case |
| `services/flow_store.py` | `test_flow_store.py` | Concurrent write/read race |
| `services/signal_store.py` | `test_signal_store.py` | Upsert collision on same ticker+ts |
| `services/stream_manager.py` | `test_stream_manager.py` | Graceful restart mid-stream |
| `services/symbols_loader.py` | `test_symbols_loader.py` | Network timeout, malformed CSV row |
| `services/tier_engine.py` | `test_tier_engine.py` + `test_4a_tier_engine.py` | Boundary premium exactly at tier threshold |
| `services/tradier_stream.py` | `test_tradier_stream.py` | Auth token refresh mid-stream |
| `services/universe_screener.py` | `test_universe_screener.py` | Zero qualifying symbols |
| `services/universe_store.py` | `test_universe_store.py` | Store full (capacity limit) |
| `parsers/options_flow_parser.py` | `test_options_flow_parser.py` + `test_occ_parser.py` | Non-standard expiry format |
| `parsers/bid_ask_classifier.py` | `test_classifier.py` | At-mid classification, zero spread |
| `signals/composite_signal_engine.py` | `test_composite_signal_engine.py` | Tie score (bull==bear) |
| `signals/repetition_accumulator.py` | `test_repetition_engine.py` | — |

---

### 🟡 Partial / Stub — Needs Expansion

| Module | Existing Test | Gaps to Fill |
|---|---|---|
| `core/auth.py` | `test_auth_cors_regression.py` | JWT decode error paths, token expiry edge, bad hash |
| `routers/auth.py` | `test_auth_flow.py` | Duplicate email on register, wrong password, `/me` with expired JWT |
| `routers/flow.py` | `test_flow_endpoint.py` | Pagination boundary, special-char ticker, unauthenticated 401, DB timeout |
| `routers/health.py` | `test_health_stream.py` | Degraded state, readiness vs liveness |
| `routers/simulation.py` | `test_simulation_and_ws.py` | Response shape, invalid ticker, auth guard |
| `routers/smart_signals.py` | `test_composite_signal_engine.py` | Endpoint auth, empty universe, score filtering, pagination |
| `routers/ws.py` | `test_simulation_and_ws.py` | WS connect/disconnect lifecycle, auth in handshake, broadcast, cleanup |
| `services/stream_worker.py` | `test_stream_worker_b008.py` | Crash + auto-restart, malformed message, backpressure |
| `services/symbol_registry.py` | `test_symbols_loader.py` (indirect) | Cache invalidation, concurrent add, unknown symbol |
| `parsers/trade_type_detector.py` | `test_classifier.py` (indirect) | Split sweep, block vs sweep threshold edge |
| `signals/backtest_validator.py` | `test_6layer_regression.py` (indirect) | Insufficient history, all-loss backtest |
| `simulation/swarm_engine.py` | `test_6layer_regression.py` (indirect) | Agent disagreement, all-HOLD outcome, exception in agent |
| `main.py` | `test_health_stream.py` + CORS tests | Startup/shutdown lifespan, rate limiting, all routers mounted |

---

### 🔴 No Tests — New Test Files Required

| Module | New Test File | Critical Cases |
|---|---|---|
| `routers/history.py` | `test_history_router.py` | GET pagination, date range, ticker filter, auth guard, DB error |
| `routers/admin.py` | `test_admin_router.py` | Admin-only 403, list users, promote/demote, delete, stream control |
| `services/demo_engine.py` | `test_demo_engine.py` | Flow generation shape, golden sweep injection, timestamp spacing |
| `services/ingestion_config.py` | `test_ingestion_config.py` | Config load, env override, missing key defaults, invalid value |
| `signals/midcap_screener.py` | `test_midcap_screener.py` | Filter logic, all midcap, no midcap edge |
| `simulation/ensemble_runner.py` | `test_ensemble_runner.py` | Aggregation, weight normalisation, empty agent list |
| `execution/trade_executor.py` | `test_trade_executor.py` | Order placement, dry-run mode, API error, position sizing |
| `config.py` | `test_config.py` | Env var parsing, missing required var raises, defaults |

---

## Frontend — Module Coverage Detail

### ✅ Covered
| Module | Test File | Remaining Gaps |
|---|---|---|
| `hooks/useAuth.ts` | `useAuth.test.ts` | Admin role isAdmin=true, register error |
| `hooks/useFlow.ts` | `useFlow.test.ts` | Auto-refetch on token change, pagination |

### 🟡 Partial
| Module | Test File | Gaps |
|---|---|---|
| `lib/api.ts` | `proxy.test.ts` | Timeout abort path, all api.* methods, non-JSON error body |
| `app/api/proxy/**` | `proxy.test.ts` | Large body passthrough, auth header forwarding |

### 🔴 No Tests — New Test Files Required
| Module | New Test File | Critical Cases |
|---|---|---|
| `app/dashboard/**` (pages) | `dashboard.test.tsx` | Auth guard redirect unauth, non-admin guard, renders with mock data |
| `app/login/**` | `login.test.tsx` | Form submit → login(), error display, redirect after login |
| `components/**` | `components.test.tsx` | FlowTable renders + empty state, SignalCard, Navbar logout |

---

## Phased Roadmap to 100%

### Phase 1 — Critical Path (Do First)
These have zero tests and cover high-risk or user-facing logic:

1. `test_history_router.py` — history endpoint is live and user-facing
2. `test_admin_router.py` — auth/permission boundary is security-critical
3. `test_config.py` — misconfigured env vars cause silent prod failures
4. `login.test.tsx` — login is the entry point to everything
5. `dashboard.test.tsx` — auth guard is the bug we just fixed

### Phase 2 — Core Logic Gaps
Expand existing partial tests to cover edge cases:

6. Expand `test_auth_flow.py` — expired JWT on `/me`, duplicate register
7. Expand `test_flow_endpoint.py` — pagination boundary, 401 path
8. Expand `test_simulation_and_ws.py` — full WS lifecycle
9. `test_demo_engine.py` — demo mode is used in prod for non-live users
10. `components.test.tsx` — FlowTable/SignalCard component smoke tests

### Phase 3 — Engine & Utility Gaps
11. `test_ensemble_runner.py`
12. `test_midcap_screener.py`
13. `test_ingestion_config.py`
14. Expand `test_6layer_regression.py` — swarm edge cases
15. Expand `lib/api.ts` — all method coverage

### Phase 4 — Execution & Boundary
16. `test_trade_executor.py` — last because it requires careful mocking of order API
17. All boundary/edge cases in covered modules (tier threshold, zero-spread, etc.)

### Phase 5 — CI Enforcement
18. Add `pytest --cov=backend --cov-fail-under=90` gate to GitHub Actions
19. Add `jest --ci --coverage --coverageThreshold='{"global":{"lines":90}}'` gate
20. Block merges to `main` if coverage drops below threshold

---

## Running the Full Suite

```bash
# Backend
cd backend
pip install -r requirements-dev.txt
pytest tests/ -v --cov=. --cov-report=term-missing

# Frontend
cd frontend
npm install
npm run test:ci   # jest --ci --coverage
```

---

## Files to Create (Phase 1 priority)

| File | Location | Priority |
|---|---|---|
| `test_history_router.py` | `backend/tests/` | P1 |
| `test_admin_router.py` | `backend/tests/` | P1 |
| `test_config.py` | `backend/tests/` | P1 |
| `test_demo_engine.py` | `backend/tests/` | P2 |
| `test_ingestion_config.py` | `backend/tests/` | P2 |
| `test_midcap_screener.py` | `backend/tests/` | P2 |
| `test_ensemble_runner.py` | `backend/tests/` | P3 |
| `test_trade_executor.py` | `backend/tests/` | P4 |
| `login.test.tsx` | `frontend/__tests__/` | P1 |
| `dashboard.test.tsx` | `frontend/__tests__/` | P1 |
| `components.test.tsx` | `frontend/__tests__/` | P2 |
