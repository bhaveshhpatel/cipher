# Regression Testing — Cipher

> Last updated: 2026-04-25 (Phase 5B)
> This document is the authoritative reference for Cipher's automated regression test suite.

---

## Overview

Cipher has a full automated regression test suite covering the entire backend and frontend
codebase. It runs automatically via GitHub Actions on every push and pull request.
Nothing merges to `main` or deploys to Railway/Vercel unless the full suite passes.

| Layer | Tool | Framework | Gate |
|---|---|---|---|
| Backend | pytest + pytest-cov | Python 3.11 | ≥ 90% coverage (`--cov-fail-under=90`) |
| Frontend | Jest + ts-jest | Node 20, jsdom | ≥ 75% lines/functions globally; ≥ 90% on `useAuth.ts` |

---

## Running Locally

### Backend

```bash
cd backend
pip install -r requirements-dev.txt

# Full suite with coverage gate
pytest

# Skip coverage for speed
pytest --no-cov

# Specific file
pytest tests/test_auth_router.py -v

# HTML coverage report
pytest --cov-report=html
open htmlcov/index.html
```

### Frontend

```bash
cd frontend
npm install

# Full suite with thresholds enforced
npx jest --coverage

# Watch mode
npx jest --watch

# CI mode
npx jest --ci --coverage
```

---

## Coverage Summary (as of 2026-04-25)

### Backend — Current Coverage: ~91%

| Module | Status | Test File(s) |
|---|---|---|
| `parsers/options_flow_parser.py` | ✅ Full | `test_tradier_stream.py` |
| `parsers/bid_ask_classifier.py` | ✅ Full | `test_tradier_stream.py` |
| `parsers/trade_type_detector.py` | ✅ Full | `test_tradier_stream.py` |
| `signals/repetition_accumulator.py` | ✅ Full | `test_tradier_stream.py` |
| `signals/composite_signal_engine.py` | ✅ Full | `test_tradier_stream.py` |
| `signals/backtest_validator.py` | ✅ Full | `test_tradier_stream.py` |
| `services/flow_store.py` | ✅ Full | `test_flow_store.py` |
| `services/signal_store.py` | ✅ Full | `test_flow_store.py` |
| `services/tradier_stream.py` | ✅ Full | `test_tradier_stream.py` |
| `services/universe_store.py` | ✅ Full | `test_universe_store.py` |
| `services/symbols_loader.py` | ✅ Full | `test_symbols_loader.py` |
| `services/tier_engine.py` | ✅ Full | `test_4a_tier_engine.py` |
| `routers/health.py` | ✅ Full | `test_health_stream.py` |
| `utils/dedup.py` | ✅ Full | `test_dedup.py` ★ P5B |
| `simulation/swarm_engine.py` | ✅ Full | `test_swarm_engine.py` ★ P5B |
| `simulation/ensemble_runner.py` | ✅ Full | `test_ensemble_runner.py` ★ P5B |
| `execution/trade_executor.py` | ✅ Full | `test_trade_executor.py` ★ P5B |
| `signals/midcap_screener.py` | ✅ Full | `test_midcap_screener.py` ★ P5B |
| `config.py` | ✅ Full | `test_config.py` ★ P5B |
| `routers/auth.py` | ✅ Full | `test_auth_router.py` ★ P5B |
| `routers/admin.py` | ✅ Full | `test_admin_router.py` ★ P5B |
| `routers/smart_signals.py` | ✅ Full | `test_smart_signals_router.py` ★ P5B |
| `routers/simulation.py` | ✅ Full | `test_simulation_router.py` ★ P5B |
| `main.py` | ✅ Full | `test_main_app.py` ★ P5B |
| `routers/history.py` | ⚠️ Partial | indirect via smart_signals tests |
| `routers/flow.py` | ⚠️ Partial | integration path only |
| `routers/ws.py` | ⚠️ Partial | ping/pong path, not load tested |
| `services/stream_manager.py` | ⚠️ Partial | lifecycle not yet tested |
| `services/stream_worker.py` | ⚠️ Partial | lifecycle not yet tested |
| `services/symbol_registry.py` | ⚠️ Partial | basic build/get |
| `utils/tradier_client.py` | ⚠️ Partial | mocked — no live calls |
| `core/auth.py` | ⚠️ Partial | covered via router tests |
| `core/async_bus.py` | ⚠️ Partial | covered via stream tests |
| `services/demo_engine.py` | ✅ Full | `test_demo_engine.py` ★ P5B |

### Frontend — Current Coverage: ~52%

| Module | Status | Test File |
|---|---|---|
| `hooks/useAuth.ts` | ✅ Full (≥90%) | `__tests__/useAuth.test.ts` |
| `hooks/useFlow.ts` | ✅ Full (≥85%) | `__tests__/useFlow.test.ts` |
| `hooks/useSignalStream.ts` | ⚠️ Partial | hook lifecycle partial |
| `hooks/useSimulation.ts` | ⚠️ Partial | mock swarm response |
| `hooks/useSignalHistory.ts` | ⚠️ Partial | basic fetch |
| `lib/api.ts` | ⚠️ Partial | endpoint coverage partial |
| `components/dashboard/SignalFeed.tsx` | ❌ None | Phase 6 target |
| `components/dashboard/FlowTable.tsx` | ❌ None | Phase 6 target |
| `components/dashboard/SimulationPanel.tsx` | ❌ None | Phase 6 target |
| `components/dashboard/CompositeCard.tsx` | ❌ None | Phase 6 target |
| `app/page.tsx` (Login) | ❌ None | Phase 6 target |
| `app/dashboard/page.tsx` | ❌ None | Phase 6 target |

---

## Test Case Inventory

### Backend (~380 total cases)

#### Phase 5B New Files

| File | ID Range | Count | Notes |
|---|---|---|---|
| `test_auth_router.py` | AUTH-01 to AUTH-15 | 15 | register, login, /me, expired JWT, missing header |
| `test_admin_router.py` | ADMIN-01 to ADMIN-12 | 12 | tier CRUD, 403 guard, cache fields |
| `test_config.py` | CFG-01 to CFG-10 | 10 | field types, defaults, key presence |
| `test_demo_engine.py` | DEMO-01 to DEMO-14 | 14 | demo mode signal shape, mock determinism |
| `test_ingestion_config.py` | ING-01 to ING-12 | 12 | ingestion toggle, env overrides |
| `test_midcap_screener.py` | MCS-01 to MCS-10 | 10 | filter thresholds, pass/fail boundaries |
| `test_ensemble_runner.py` | ENS-01 to ENS-18 | 18 | majority vote, tie-breaking, name field |
| `test_dedup.py` | DEDUP-01 to DEDUP-22 | 22 | TTL, sweep detection, singleton |
| `test_swarm_engine.py` | SWM-01 to SWM-25 | 25 | all 12 roles, HOLD fallback, confidence |
| `test_trade_executor.py` | TE-01 to TE-14 | 14 | market/limit/error/network, OCC root |
| `test_simulation_router.py` | SIM-01 to SIM-12 | 12 | validation, 422 bounds, serialisation |
| `test_smart_signals_router.py` | SS-01 to SS-16 | 16 | DB hit/miss, filters, _row_to_composite |
| `test_main_app.py` | MAIN-01 to MAIN-15 | 15 | /health, routers, _JsonFormatter, _stamp_oi |

#### Pre-existing Files

| File | Approx Cases | Notes |
|---|---|---|
| `test_symbols_loader.py` | ~30 | CBOE fetch, validation, batch quotes |
| `test_tradier_stream.py` | ~35 | stream lifecycle, parsers, reconnect |
| `test_flow_store.py` | ~25 | Supabase writes, service key, RLS |
| `test_universe_store.py` | ~20 | snapshot read/write, upsert |
| `test_4a_tier_engine.py` | ~22 | tier assign, classify, thresholds |
| `test_health_stream.py` | ~8 | stream health endpoint |

### Frontend

| File | Count | Notes |
|---|---|---|
| `__tests__/useAuth.test.ts` | ~20 | login, logout, token refresh, expired |
| `__tests__/useFlow.test.ts` | ~18 | flow fetch, filters, pagination |
| `__tests__/useSignalStream.test.ts` | ~12 | WS connect, pong, reconnect |
| `__tests__/useSimulation.test.ts` | ~10 | POST /simulate, n_agents |
| `__tests__/useSignalHistory.test.ts` | ~10 | GET /api/signals/history, filters |
| `__tests__/api.test.ts` | ~15 | client methods, error handling |

---

## CI Configuration Files

### `backend/pytest.ini`

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
addopts =
    --cov=.
    --cov-report=term-missing
    --cov-report=xml:coverage.xml
    --cov-report=html:htmlcov
    --cov-fail-under=90
    -x
markers =
    regression: fast regression tests
    slow: slow integration tests
```

### `backend/.coveragerc`

```ini
[run]
source = .
omit =
    tests/*
    migrations/*
    .venv/*
    */__pycache__/*
    */site-packages/*

[report]
exclude_lines =
    pragma: no cover
    def __repr__
    if TYPE_CHECKING:
    raise NotImplementedError
    @abstractmethod

[coverage:run]
fail_under = 90
```

### `frontend/jest.config.ts`

```typescript
import type { Config } from 'jest';

const config: Config = {
  testEnvironment: 'jsdom',
  preset: 'ts-jest',
  moduleNameMapper: {
    '\\.(css|less|scss|sass)$': '<rootDir>/__mocks__/styleMock.ts',
    '\\.(jpg|jpeg|png|gif|svg)$': '<rootDir>/__mocks__/fileMock.ts',
    '^@/(.*)$': '<rootDir>/src/$1',
  },
  collectCoverageFrom: [
    'src/**/*.{ts,tsx}',
    '!src/**/*.d.ts',
    '!src/**/index.ts',
  ],
  coverageThreshold: {
    global: {
      lines: 75,
      functions: 75,
      branches: 70,
      statements: 75,
    },
    './src/hooks/useAuth.ts': {
      lines: 90,
      functions: 90,
    },
    './src/hooks/useFlow.ts': {
      lines: 85,
      functions: 85,
    },
  },
};

export default config;
```

---

## GitHub Actions Workflows

### Backend (`.github/workflows/backend.yml`)

```yaml
name: Backend CI

on:
  push:
    paths: ['backend/**']
  pull_request:
    paths: ['backend/**']

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - run: pip install flake8
      - run: flake8 backend/ --count --select=E9,F63,F7,F82 --show-source --statistics

  regression:
    needs: lint
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: backend
    env:
      SECRET_KEY: test-secret-key-for-ci
      ALGORITHM: HS256
      ACCESS_TOKEN_EXPIRE_MINUTES: "1440"
      SUPABASE_URL: https://placeholder.supabase.co
      SUPABASE_KEY: placeholder-anon-key
      SUPABASE_SERVICE_KEY: placeholder-service-key
      TRADIER_API_KEY: placeholder-tradier-key
      TRADIER_ACCOUNT_ID: placeholder-account-id
      GROQ_API_KEY: ""
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest
      - uses: actions/upload-artifact@v4
        with:
          name: backend-coverage
          path: backend/coverage.xml
      - uses: orgoro/coverage@v3.2
        if: github.event_name == 'pull_request'
        with:
          coverageFile: backend/coverage.xml
          token: ${{ secrets.GITHUB_TOKEN }}
          thresholdAll: 0.90
```

### Frontend (`.github/workflows/frontend.yml`)

```yaml
name: Frontend CI

on:
  push:
    paths: ['frontend/**']
  pull_request:
    paths: ['frontend/**']

jobs:
  typecheck:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
      - run: npx tsc --noEmit

  regression:
    needs: typecheck
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
      - run: npx jest --ci --coverage
      - uses: actions/upload-artifact@v4
        with:
          name: frontend-coverage
          path: frontend/coverage/

  build:
    needs: regression
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    env:
      NEXT_PUBLIC_API_URL: ${{ secrets.NEXT_PUBLIC_API_URL }}
      NEXT_PUBLIC_WS_URL: ${{ secrets.NEXT_PUBLIC_WS_URL }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
      - run: npm run build

  deploy:
    needs: build
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - run: npm ci
      - run: npx vercel --prod --token=${{ secrets.VERCEL_TOKEN }}
        env:
          VERCEL_ORG_ID: ${{ secrets.VERCEL_ORG_ID }}
          VERCEL_PROJECT_ID: ${{ secrets.VERCEL_PROJECT_ID }}
```

---

## Phased Plan to 100%

| Phase | Status | Focus | Coverage Target |
|---|---|---|---|
| P1 | ✅ Done | Auth, admin, config, demo | ~68% |
| P2 | ✅ Done | Dedup, swarm, ensemble, screener | ~82% |
| P3 | ✅ Done | Trade executor, routers, main | ~91% |
| P4 | 🔄 In Progress | history/flow/ws full coverage | ~96% |
| P5 | 🔲 Planned | Frontend UI components | ~100% |

### P4 Remaining Files
- `test_history_router.py` — full `/api/signals/history` endpoint coverage
- `test_flow_router.py` — `/api/flow/scan`, episodes query, pagination
- `test_ws_router.py` — WS lifecycle, pong timeout, 4001 on bad JWT

### P5 Remaining Files
- `SignalFeed.test.tsx` — render, ticker filter, real-time update
- `FlowTable.test.tsx` — rows, pagination, sort
- `SimulationPanel.test.tsx` — n_agents input, run, result render
- `login.test.tsx` — form submit, redirect, error state
- `dashboard.test.tsx` — tab navigation, auth guard

---

## Coverage Gate Rules

| Gate | Tool | Value | Fail Behaviour |
|---|---|---|---|
| Backend line coverage | `pytest --cov-fail-under` | ≥ 90% | CI job fails, merge blocked |
| Frontend global lines | `jest coverageThreshold` | ≥ 75% | CI job fails, deploy blocked |
| Frontend global functions | `jest coverageThreshold` | ≥ 75% | CI job fails, deploy blocked |
| `useAuth.ts` lines | `jest coverageThreshold` | ≥ 90% | CI job fails, deploy blocked |
| `useFlow.ts` lines | `jest coverageThreshold` | ≥ 85% | CI job fails, deploy blocked |

> **Phase 6 target:** Raise backend to `--cov-fail-under=95` and frontend global to `80%` once UI component tests are complete.
