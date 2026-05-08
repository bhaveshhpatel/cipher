/**
 * useGatePatch.ts — Sends a PATCH request to update a single gate value.
 * ADMIN-UI-001 | Chunk 2
 *
 * PATCH /api/admin/gate-config → PatchGateResponse
 *
 * Returns a `patch()` and `confirmPatch()` function plus a `SaveStatus`
 * per (gate_name, tier) cell.
 *
 * 428 flow:
 *   1. patch() fires without confirm_market_hours.
 *   2. Backend returns 428 → status set to "market_confirm".
 *   3. UI shows inline amber banner with Confirm / Cancel.
 *   4. Admin clicks Confirm → confirmPatch() re-sends with
 *      confirm_market_hours: true.
 *   5. Admin clicks Cancel  → cancelConfirm() resets status to idle,
 *      draft stays at the edited value so they can adjust.
 *
 * Error status (non-428) persists until the next patch attempt.
 * Saved status auto-resets to idle after 2.5 s.
 */
"use client";
import { useState, useCallback, useRef } from "react";
import type { PatchGatePayload, PatchGateResponse, SaveStatus } from "@/types/gates";
import { GATE_CONFIG_URL } from "./useGateConfig";

export type StatusMap  = Record<string, SaveStatus>;
export type ErrorMap   = Record<string, string>;
// Stores the pending payload for cells awaiting market-hours confirmation.
type PendingMap = Record<string, { token: string; payload: PatchGatePayload }>;

export interface UseGatePatchReturn {
  statusMap:     StatusMap;
  errorMap:      ErrorMap;
  patch:         (token: string, payload: PatchGatePayload) => Promise<PatchGateResponse | null>;
  confirmPatch:  (gateName: string, tier: 1 | 2 | 3) => Promise<PatchGateResponse | null>;
  cancelConfirm: (gateName: string, tier: 1 | 2 | 3) => void;
}

function cellKey(gateName: string, tier: 1 | 2 | 3): string {
  return `${gateName}:${tier}`;
}

/**
 * Safely extract a human-readable message from an API error body.
 * FastAPI 422 responses return `detail` as an array of validation objects;
 * all other errors return `detail` as a plain string.
 */
function extractDetail(body: unknown, fallback: string): string {
  if (!body || typeof body !== "object") return fallback;
  const detail = (body as Record<string, unknown>).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as Record<string, unknown>;
    const msg   = typeof first.msg === "string" ? first.msg : JSON.stringify(first);
    return detail.length > 1 ? `${msg} (+${detail.length - 1} more)` : msg;
  }
  return fallback;
}

async function sendPatch(
  token: string,
  payload: PatchGatePayload,
): Promise<{ ok: true; result: PatchGateResponse } | { ok: false; status: number; msg: string }> {
  const res = await fetch(GATE_CONFIG_URL, {
    method:  "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization:  `Bearer ${token}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    return { ok: false, status: res.status, msg: extractDetail(body, `HTTP ${res.status}`) };
  }
  const result: PatchGateResponse = await res.json();
  return { ok: true, result };
}

export function useGatePatch(): UseGatePatchReturn {
  const [statusMap, setStatusMap] = useState<StatusMap>({});
  const [errorMap,  setErrorMap]  = useState<ErrorMap>({});
  const pendingRef = useRef<PendingMap>({});

  const setStatus = useCallback((key: string, status: SaveStatus) => {
    setStatusMap(prev => ({ ...prev, [key]: status }));
  }, []);

  const setError = useCallback((key: string, msg: string) => {
    setErrorMap(prev => ({ ...prev, [key]: msg }));
  }, []);

  const clearError = useCallback((key: string) => {
    setErrorMap(prev => { const n = { ...prev }; delete n[key]; return n; });
  }, []);

  /** Initial save attempt — does NOT include confirm_market_hours. */
  const patch = useCallback(
    async (token: string, payload: PatchGatePayload): Promise<PatchGateResponse | null> => {
      const key = cellKey(payload.gate_name, payload.tier);
      setStatus(key, "saving");
      clearError(key);

      const res = await sendPatch(token, { ...payload, confirm_market_hours: false });

      if (!res.ok) {
        if (res.status === 428) {
          // Market is open — park the payload and wait for admin confirmation.
          pendingRef.current[key] = { token, payload };
          setStatus(key, "market_confirm");
          return null;
        }
        setStatus(key, "error");
        setError(key, res.msg);
        throw new Error(res.msg);
      }

      setStatus(key, "saved");
      setTimeout(() => setStatus(key, "idle"), 2_500);
      return res.result;
    },
    [setStatus, setError, clearError],
  );

  /** Re-sends the parked payload with confirm_market_hours: true. */
  const confirmPatch = useCallback(
    async (gateName: string, tier: 1 | 2 | 3): Promise<PatchGateResponse | null> => {
      const key     = cellKey(gateName, tier);
      const pending = pendingRef.current[key];
      if (!pending) return null;

      setStatus(key, "saving");
      clearError(key);
      delete pendingRef.current[key];

      const res = await sendPatch(pending.token, { ...pending.payload, confirm_market_hours: true });

      if (!res.ok) {
        setStatus(key, "error");
        setError(key, res.msg);
        throw new Error(res.msg);
      }

      setStatus(key, "saved");
      setTimeout(() => setStatus(key, "idle"), 2_500);
      return res.result;
    },
    [setStatus, setError, clearError],
  );

  /** Dismisses the 428 confirmation prompt without re-sending. */
  const cancelConfirm = useCallback(
    (gateName: string, tier: 1 | 2 | 3) => {
      const key = cellKey(gateName, tier);
      delete pendingRef.current[key];
      setStatus(key, "idle");
      clearError(key);
    },
    [setStatus, clearError],
  );

  return { statusMap, errorMap, patch, confirmPatch, cancelConfirm };
}
