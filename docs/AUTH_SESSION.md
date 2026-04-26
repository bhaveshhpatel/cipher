# Auth Session Handling

## Overview

`useAuth` manages authentication state with an **optimistic-then-validate** pattern:

1. On mount it reads `cipher_token`, `cipher_email`, `cipher_role` from `localStorage` and immediately sets `isAuthenticated: true` + `ready: true` so the dashboard renders without a loading flash.
2. It then fires `fetchMe()` asynchronously to validate the token with the backend `/api/auth/me`.

---

## Ghost Session Bug (Fixed — April 2026)

### Symptom
Opening the app redirected correctly to `/dashboard`, but the Flow Scanner table showed an empty state or "session expired" error. The user had to manually sign out and sign back in.

### Root Cause
`fetchMe()` previously had a silent `return` on non-ok responses:

```typescript
// BEFORE (broken)
if (!res.ok) return; // silently ignored 401 — token stayed in state
```

An expired JWT returns HTTP `401` from `/me`. Because the failure was swallowed, `isAuthenticated` stayed `true`, the dashboard guard never redirected to login, and all subsequent API calls (e.g. `useFlow → api.getFlow`) fired with the dead token and received `401` errors — surfacing as "session expired" in the UI.

### Fix
`fetchMe()` now distinguishes between error types:

| `/me` Response | Behaviour |
|---|---|
| `401 Unauthorized` | **Hard logout** — clears localStorage, sets `isAuthenticated: false`, dashboard guard redirects to `/login` |
| Other non-2xx (e.g. `503`) | **Non-fatal** — leaves cached session intact (network blip / cold start) |
| Network exception | **Non-fatal** — leaves cached session intact |
| `200 OK` | Refreshes `email` + `role` from server |

```typescript
// AFTER (fixed) — in fetchMe()
if (res.status === 401) {
  clearStorage();
  setState(s => ({ ...s, token: null, email: null, role: null,
                   isAuthenticated: false, isAdmin: false, ready: true }));
  return;
}
if (!res.ok) return; // non-auth errors: non-fatal, keep session
```

---

## State Shape

```typescript
interface AuthState {
  token:           string | null;
  email:           string | null;
  role:            string | null;
  isAuthenticated: boolean;
  isAdmin:         boolean;   // true if role === "admin" | "founder"
  ready:           boolean;   // true once localStorage read + guard can fire
  loading:         boolean;   // true during login/register requests
  error:           string | null;
}
```

---

## Auth Flow Diagram

```
Mount
 └─ localStorage has token?
      ├─ NO  → ready=true, isAuthenticated=false → dashboard guard → /login
      └─ YES → optimistic: isAuthenticated=true, ready=true → dashboard renders
                └─ fetchMe(token) async
                     ├─ 401 → clearStorage(), isAuthenticated=false → guard → /login
                     ├─ 5xx / network error → no-op (keep session)
                     └─ 200 → update email + role in state
```

---

## localStorage Keys

| Key | Value |
|---|---|
| `cipher_token` | JWT access token |
| `cipher_email` | User email (cache) |
| `cipher_role` | `"user"` \| `"admin"` \| `"founder"` |

All three are cleared together on logout or 401.

---

## Testing

Tests live in `frontend/__tests__/useAuth.test.ts`. Run with:

```bash
cd frontend
npm install        # installs jest, ts-jest, @testing-library/react
npm test           # jest --passWithNoTests
npm run test:ci    # jest --ci --coverage
```

### Test Cases

| Test | What It Validates |
|---|---|
| Empty localStorage → unauthenticated | No ghost session on fresh load |
| Token in localStorage → optimistic auth | Dashboard renders without flicker |
| `/me` returns 401 → auto-logout | **Core bug fix** — expired token clears state |
| `/me` returns 503 → session preserved | Network blip doesn't log you out |
| `/me` throws network error → session preserved | Offline tolerance |
| Login success | Token + email + role persisted |
| Login with bad credentials | `error` state set, not authenticated |
| Logout | State + localStorage cleared |

---

## Related Files

| File | Role |
|---|---|
| `frontend/src/hooks/useAuth.ts` | Auth state hook |
| `frontend/src/hooks/useFlow.ts` | Uses `token` from useAuth to fetch flow events |
| `frontend/__tests__/useAuth.test.ts` | Unit tests |
| `frontend/jest.config.ts` | Jest configuration (ts-jest + jsdom) |
| `frontend/jest.setup.ts` | Loads `@testing-library/jest-dom` matchers |
