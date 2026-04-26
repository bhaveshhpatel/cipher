# Cipher — Regression Testing Guide

This document explains the full regression test suite, how to run it locally,
and how the CI enforcement gates work.

---

## Quick Start

### Backend
```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
pytest
```
This runs all tests with coverage. The gate is **92% total, branch coverage enabled**.
A failing test or coverage drop below 92% exits with a non-zero code.

### Frontend
```bash
cd frontend
npm ci
npx jest --coverage
```
This runs all Jest tests with coverage thresholds enforced per `jest.config.ts`.

---

## Running a Subset

```bash
# Backend — single file
pytest tests/test_auth_router.py -v

# Backend — by marker
pytest -m regression

# Backend — skip slow tests
pytest -m "not slow"

# Frontend — single file
npx jest src/hooks/useAuth.test.ts --coverage
```

---

## Coverage Gates

### Backend (`pytest.ini` + `.coveragerc`)

| Metric | Threshold |
|---|---|
| Total line coverage | ≥ 92% |
| Branch coverage | Enabled (measured, not separately gated) |
| New files on PR | ≥ 90% (via `orgoro/coverage` action) |
| Modified files on PR | ≥ 85% |

### Frontend (`jest.config.ts`)

| Scope | Branches | Functions | Lines | Statements |
|---|---|---|---|---|
| Global (all `src/`) | 80% | 85% | 85% | 85% |
| `src/hooks/useAuth.ts` | 95% | 95% | 95% | 95% |
| `src/hooks/useFlow.ts` | 90% | 90% | 90% | 90% |
| `src/lib/api.ts` | 80% | 80% | 80% | 80% |

---

## CI Workflows

### `regression-gate.yml` (required check)
Triggered on every PR to `main`. Runs backend + frontend in parallel.
Both must pass before GitHub allows the merge button.

To enforce it:
1. Go to **GitHub → Settings → Branches → main → Branch protection rules**
2. Enable **Require status checks to pass before merging**
3. Add `All Regression Gates Passed` as a required check

### `backend.yml`
Triggered on push/PR when `backend/**` changes. Runs lint + regression.

### `frontend.yml`
Triggered on push/PR when `frontend/**` changes.
Pipeline: typecheck → regression → build → deploy (main only).

---

## Test Suite Map

### Backend (350 tests across 18 files)

| File | Phase | Tests |
|---|---|---|
| `test_auth_router.py` | P1 | Auth endpoints, JWT validation, refresh, /me |
| `test_admin_router.py` | P1 | Admin endpoints, 403 guard tests |
| `test_history_router.py` | P1 | History endpoint, DB queries |
| `test_config.py` | P1 | Settings, env var validation |
| `test_demo_engine.py` | P2 | Demo mode flow events |
| `test_ingestion_config.py` | P2 | Ticker + ingestion config |
| `test_midcap_screener.py` | P2 | Mid-cap screening logic |
| `test_ws_router.py` | P3 | WebSocket auth, heartbeat, pong timeout |
| `test_simulation_router.py` | P3 | Simulation endpoint, n_agents/n_runs validation |
| `test_smart_signals_router.py` | P3 | Composite signals, mock vs live source |
| `test_ensemble_runner.py` | P4 | Vote aggregation, confidence, agents list |
| `test_swarm_engine.py` | P4 | Agent snapping, flow summary, LLM parsing |
| `test_trade_executor.py` | P4 | HTTP order placement, error handling |
| `test_flow_store.py` | Existing | Flow event CRUD |
| `test_signal_store.py` | Existing | Signal CRUD |
| `test_tier_engine.py` | Existing | Tier classification |
| `test_tradier_stream.py` | Existing | Stream parsing |
| `test_parsers.py` | Existing | Event parsing |

### Frontend (hooks + pages + components)

| File | Tests |
|---|---|
| `useAuth.test.ts` | Token management, refresh, expiry |
| `useFlow.test.ts` | Flow event subscription, state updates |
| `login.test.tsx` | Login form, error states, redirect |
| `dashboard.test.tsx` | Dashboard render, auth guard |
| `components.test.tsx` | Shared UI components |
| `api.test.ts` | API client, error handling |

---

## Adding a New Test

1. **Backend**: Create `backend/tests/test_<module>.py`. Follow the existing
   pattern — patch external I/O, no real HTTP/DB calls in unit tests.
   Mark with `@pytest.mark.regression`.

2. **Frontend**: Create `src/**/__tests__/<Component>.test.tsx` or
   `src/hooks/<hook>.test.ts`. Mock `fetch` via `jest.fn()` or `msw`.

3. The CI gate will automatically pick up the new file and enforce thresholds.

---

## Generating a Local HTML Report

```bash
# Backend
cd backend
pytest --cov-report=html:coverage_html
open coverage_html/index.html

# Frontend
cd frontend
npx jest --coverage --coverageReporters=html
open coverage/index.html
```
