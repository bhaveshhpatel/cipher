/**
 * Cipher shared formatters.
 * All functions are pure — no side-effects, no imports.
 * Exported individually so tree-shaking works in tests.
 */

/** Format uptime in seconds to human-readable string.
 *  @example fmtUptime(3723) → "1h 2m 3s" */
export function fmtUptime(seconds: number | null | undefined): string {
  if (seconds == null || isNaN(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  return `${h}h ${m}m ${rem}s`;
}

/** Format an ISO timestamp to ET time string.
 *  Returns "—" for null/undefined/empty.
 *  Returns the raw string if timestamp is unparseable (isNaN check, not try/catch).
 *  @example fmtTime("2026-04-29T13:45:00Z") → "09:45:00 ET" */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour12:   false,
    hour:     "2-digit",
    minute:   "2-digit",
    second:   "2-digit",
  }) + " ET";
}

/** Format a number with commas; abbreviate to K or M.
 *  Works correctly for negative numbers.
 *  @example fmtNumber(1234567) → "1.2M"  fmtNumber(-1500) → "-1.5K" */
export function fmtNumber(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_000_000) return `${sign}${(abs / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000)     return `${sign}${(abs / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

/** Format a dollar amount with commas; abbreviate to K or M.
 *  @example fmtDollar(1500000) → "$1.5M" */
export function fmtDollar(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "—";
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000)     return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

/** Format a decimal (0–1) as a percentage string.
 *  Accepts both 0.625 and 62.5 — values >1 are treated as already-percent.
 *  @example fmtPct(0.625) → "62.5%" */
export function fmtPct(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "—";
  const pct = Math.abs(n) <= 1 ? n * 100 : n;
  return `${pct.toFixed(1)}%`;
}

/** Format a signed delta with leading +/− sign.
 *  Zero returns "—".
 *  @example fmtDelta(234) → "+234"  fmtDelta(-12) → "−12" */
export function fmtDelta(n: number | null | undefined): string {
  if (n == null || isNaN(n) || n === 0) return "—";
  return n > 0 ? `+${fmtNumber(n)}` : `−${fmtNumber(Math.abs(n))}`;
}

/** Format an ISO timestamp as relative time from now.
 *  @example fmtRelativeTime("2026-04-29T13:40:00Z") → "5 minutes ago" */
export function fmtRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const diffMs = Date.now() - d.getTime();
  if (diffMs < 0) return "just now";
  const secs  = Math.floor(diffMs / 1_000);
  const mins  = Math.floor(secs  / 60);
  const hours = Math.floor(mins  / 60);
  const days  = Math.floor(hours / 24);
  if (secs  < 30)  return "just now";
  if (secs  < 90)  return "1 minute ago";
  if (mins  < 60)  return `${mins} minutes ago`;
  if (hours < 2)   return "1 hour ago";
  if (hours < 24)  return `${hours} hours ago`;
  if (days  < 2)   return "1 day ago";
  return `${days} days ago`;
}

/** Format seconds to duration string (no hours component).
 *  @example fmtDuration(185) → "3m 5s" */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || isNaN(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}
