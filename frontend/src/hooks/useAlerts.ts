/**
 * useAlerts — SWR hook for user alert configurations.
 *
 * Fetches /api/proxy/alerts (GET) and exposes
 * create / update / remove mutators that optimistically update the cache.
 */
import useSWR from "swr";
import type { Alert } from "@/types";

async function fetcher<T>(url: string): Promise<T> {
  const token = typeof window !== "undefined"
    ? localStorage.getItem("cipher_token")
    : null;

  const res = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });

  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`) as Error & { status: number };
    err.status = res.status;
    throw err;
  }

  return res.json() as Promise<T>;
}

async function mutationFetch<T>(
  url: string,
  method: "POST" | "PATCH" | "DELETE",
  body?: unknown,
): Promise<T> {
  const token = typeof window !== "undefined"
    ? localStorage.getItem("cipher_token")
    : null;

  const res = await fetch(url, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`) as Error & { status: number };
    err.status = res.status;
    throw err;
  }

  return res.json() as Promise<T>;
}

const ALERTS_KEY = "/api/proxy/alerts";

export interface UseAlertsReturn {
  alerts:       Alert[];
  isLoading:    boolean;
  error:        Error | null;
  create:       (payload: Omit<Alert, "id" | "created_at">) => Promise<void>;
  update:       (id: string, patch: Partial<Omit<Alert, "id" | "created_at">>) => Promise<void>;
  remove:       (id: string) => Promise<void>;
  refresh:      () => void;
}

export function useAlerts(): UseAlertsReturn {
  const { data, error, isLoading, mutate } =
    useSWR<{ alerts: Alert[] }>(ALERTS_KEY, fetcher, {
      revalidateOnFocus:     false,
      revalidateOnReconnect: true,
    });

  const alerts = data?.alerts ?? [];

  const create = async (payload: Omit<Alert, "id" | "created_at">) => {
    // Optimistic insert with temp id
    const temp: Alert = { ...payload, id: `temp-${Date.now()}`, created_at: new Date().toISOString() };
    await mutate(
      async (cur) => {
        const created = await mutationFetch<Alert>(ALERTS_KEY, "POST", payload);
        return { alerts: [...(cur?.alerts ?? []), created] };
      },
      { optimisticData: { alerts: [...alerts, temp] }, rollbackOnError: true },
    );
  };

  const update = async (id: string, patch: Partial<Omit<Alert, "id" | "created_at">>) => {
    await mutate(
      async (cur) => {
        const updated = await mutationFetch<Alert>(`${ALERTS_KEY}/${id}`, "PATCH", patch);
        return { alerts: (cur?.alerts ?? []).map(a => a.id === id ? updated : a) };
      },
      {
        optimisticData: { alerts: alerts.map(a => a.id === id ? { ...a, ...patch } : a) },
        rollbackOnError: true,
      },
    );
  };

  const remove = async (id: string) => {
    await mutate(
      async (cur) => {
        await mutationFetch<void>(`${ALERTS_KEY}/${id}`, "DELETE");
        return { alerts: (cur?.alerts ?? []).filter(a => a.id !== id) };
      },
      {
        optimisticData: { alerts: alerts.filter(a => a.id !== id) },
        rollbackOnError: true,
      },
    );
  };

  return {
    alerts,
    isLoading,
    error:   error ?? null,
    create,
    update,
    remove,
    refresh: () => mutate(),
  };
}
