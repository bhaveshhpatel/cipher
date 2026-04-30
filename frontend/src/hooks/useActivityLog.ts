/**
 * useActivityLog.ts  [STORY-BE-001]
 *
 * SWR hook for GET /api/admin/activity-log.
 * Returns (items, total, count, loading, error, refresh).
 * Uses keepPreviousData so the table doesn't flash on filter/page changes.
 */
import useSWR from "swr";

export interface ActivityLogItem {
  id:          string;
  created_at:  string;
  admin_email: string;
  action:      string;
  detail:      Record<string, unknown>;
  ip_address:  string | null;
}

interface ActivityLogResponse {
  limit:  number;
  offset: number;
  total:  number;
  count:  number;
  items:  ActivityLogItem[];
}

export interface UseActivityLogParams {
  token:       string | null;
  limit?:      number;
  offset?:     number;
  action?:     string | null;
  adminEmail?: string | null;
  since?:      string | null;
  before?:     string | null;
  paused?:     boolean;
}

async function fetcher([url, token]: [string, string]): Promise<ActivityLogResponse> {
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) {
    const err = Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
    throw err;
  }
  return res.json();
}

export function useActivityLog({
  token,
  limit      = 20,
  offset     = 0,
  action     = null,
  adminEmail = null,
  since      = null,
  before     = null,
  paused     = false,
}: UseActivityLogParams) {
  const params = new URLSearchParams();
  params.set("limit",  String(limit));
  params.set("offset", String(offset));
  if (action)     params.set("action",      action);
  if (adminEmail) params.set("admin_email", adminEmail);
  if (since)      params.set("since",       since);
  if (before)     params.set("before",      before);

  const key = !paused && token
    ? [`/api/admin/activity-log?${params.toString()}`, token] as [string, string]
    : null;

  const { data, error, isLoading, mutate } = useSWR<ActivityLogResponse>(
    key,
    fetcher,
    { keepPreviousData: true, refreshInterval: 30_000 },
  );

  return {
    items:   data?.items   ?? [],
    total:   data?.total   ?? 0,
    count:   data?.count   ?? 0,
    loading: isLoading,
    error:   error?.message ?? null,
    refresh: mutate,
  };
}
