/**
 * useGatePatch.ts — Sends a PATCH request to update a single gate value.
 * ADMIN-UI-001 | Chunk 2
 *
 * PATCH /api/admin/gate-config → PatchGateResponse
 *
 * Returns a `patch()` function and a `SaveStatus` per (gate_name, tier) cell.
 * Multiple patches can be in-flight simultaneously; status is keyed by
 * `${gate_name}:${tier}` so each cell has independent saving/saved/error state.
 *
 * Error status persists until the next patch attempt on the same cell, giving
 * the admin a persistent visual signal that the last write failed.
 * Saved status auto-resets to idle after 2.5 s.
 */
"use client";
import { useState, useCallback } from "react";
import type { PatchGatePayload, PatchGateResponse, SaveStatus } from "@/types/gates";
import { GATE_CONFIG_URL } from "./useGateConfig";

export type StatusMap = Record<string, SaveStatus>;

export interface UseGatePatchReturn {
  statusMap: StatusMap;
  patch:     (token: string, payload: PatchGatePayload) => Promise<PatchGateResponse | null>;
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
    // FastAPI validation error: [{loc, msg, type}, ...]
    const first = detail[0] as Record<string, unknown>;
    const msg   = typeof first.msg === "string" ? first.msg : JSON.stringify(first);
    return detail.length > 1 ? `${msg} (+${detail.length - 1} more)` : msg;
  }
  return fallback;
}

export function useGatePatch(): UseGatePatchReturn {
  const [statusMap, setStatusMap] = useState<StatusMap>({});

  const setStatus = useCallback((key: string, status: SaveStatus) => {
    setStatusMap(prev => ({ ...prev, [key]: status }));
  }, []);

  const patch = useCallback(
    async (token: string, payload: PatchGatePayload): Promise<PatchGateResponse | null> => {
      const key = cellKey(payload.gate_name, payload.tier);
      setStatus(key, "saving");
      try {
        const res = await fetch(GATE_CONFIG_URL, {
          method:  "PATCH",
          headers: {
            "Content-Type":  "application/json",
            Authorization:   `Bearer ${token}`,
          },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          const msg  = extractDetail(body, `HTTP ${res.status}`);
          setStatus(key, "error");
          throw new Error(msg);
        }
        const result: PatchGateResponse = await res.json();
        setStatus(key, "saved");
        // Auto-reset saved → idle after 2.5 s so the ✓ flash disappears.
        // Error status intentionally does NOT auto-reset — it persists until
        // the admin retries so they can't miss a failed write.
        setTimeout(() => setStatus(key, "idle"), 2_500);
        return result;
      } catch (e: unknown) {
        setStatus(key, "error");
        throw e;
      }
    },
    [setStatus],
  );

  return { statusMap, patch };
}
