import {
  fmtUptime,
  fmtTime,
  fmtNumber,
  fmtDollar,
  fmtPct,
  fmtDelta,
  fmtRelativeTime,
  fmtDuration,
} from "@/lib/formatters";

// ── fmtUptime ──
describe("fmtUptime", () => {
  it("returns — for null",      () => expect(fmtUptime(null)).toBe("—"));
  it("returns — for undefined", () => expect(fmtUptime(undefined)).toBe("—"));
  it("returns — for NaN",       () => expect(fmtUptime(NaN)).toBe("—"));
  it("returns — for negative",  () => expect(fmtUptime(-5)).toBe("—"));
  it("formats 0s",              () => expect(fmtUptime(0)).toBe("0s"));
  it("formats 45s",             () => expect(fmtUptime(45)).toBe("45s"));
  it("formats 59s",             () => expect(fmtUptime(59)).toBe("59s"));
  it("formats 60s as 1m 0s",    () => expect(fmtUptime(60)).toBe("1m 0s"));
  it("formats 90s",             () => expect(fmtUptime(90)).toBe("1m 30s"));
  it("formats 3600s as 1h 0m 0s", () => expect(fmtUptime(3600)).toBe("1h 0m 0s"));
  it("formats 3723s",           () => expect(fmtUptime(3723)).toBe("1h 2m 3s"));
  it("floors fractional seconds", () => expect(fmtUptime(61.9)).toBe("1m 1s"));
});

// ── fmtTime ──
describe("fmtTime", () => {
  it("returns — for null",      () => expect(fmtTime(null)).toBe("—"));
  it("returns — for undefined", () => expect(fmtTime(undefined)).toBe("—"));
  it("returns — for empty",    () => expect(fmtTime("")).toBe("—"));
  it("formats a valid ISO timestamp as ET time string", () => {
    const result = fmtTime("2026-04-29T14:00:00Z"); // 10:00 ET
    expect(result).toMatch(/ET$/);
    expect(result).toMatch(/^\d{2}:\d{2}:\d{2} ET$/);
  });
  it("returns the raw string if timestamp is invalid", () => {
    expect(fmtTime("not-a-date")).toBe("not-a-date");
  });
});

// ── fmtNumber ──
describe("fmtNumber", () => {
  it("returns — for null",      () => expect(fmtNumber(null)).toBe("—"));
  it("returns — for undefined", () => expect(fmtNumber(undefined)).toBe("—"));
  it("returns — for NaN",       () => expect(fmtNumber(NaN)).toBe("—"));
  it("formats 0",               () => expect(fmtNumber(0)).toBe("0"));
  it("formats 999",             () => expect(fmtNumber(999)).toBe("999"));
  it("formats 1000 as 1.0K",    () => expect(fmtNumber(1000)).toBe("1.0K"));
  it("formats 1500 as 1.5K",    () => expect(fmtNumber(1500)).toBe("1.5K"));
  it("formats 999999 as 1000.0K", () => expect(fmtNumber(999_999)).toBe("1000.0K"));
  it("formats 1000000 as 1.0M", () => expect(fmtNumber(1_000_000)).toBe("1.0M"));
  it("formats 2500000 as 2.5M", () => expect(fmtNumber(2_500_000)).toBe("2.5M"));
  it("handles negative",        () => expect(fmtNumber(-1500)).toBe("-1.5K"));
});

// ── fmtDollar ──
describe("fmtDollar", () => {
  it("returns — for null",      () => expect(fmtDollar(null)).toBe("—"));
  it("returns — for NaN",       () => expect(fmtDollar(NaN)).toBe("—"));
  it("formats 500 as $500",     () => expect(fmtDollar(500)).toBe("$500"));
  it("formats 1500 as $1.5K",   () => expect(fmtDollar(1500)).toBe("$1.5K"));
  it("formats 1500000 as $1.50M", () => expect(fmtDollar(1_500_000)).toBe("$1.50M"));
  it("formats 2000000 as $2.00M", () => expect(fmtDollar(2_000_000)).toBe("$2.00M"));
});

// ── fmtPct ──
describe("fmtPct", () => {
  it("returns — for null",      () => expect(fmtPct(null)).toBe("—"));
  it("returns — for NaN",       () => expect(fmtPct(NaN)).toBe("—"));
  it("formats 0 as 0.0%",       () => expect(fmtPct(0)).toBe("0.0%"));
  it("formats 0.625 as 62.5%",  () => expect(fmtPct(0.625)).toBe("62.5%"));
  it("formats 1.0 as 100.0%",   () => expect(fmtPct(1.0)).toBe("100.0%"));
  it("formats 62.5 (already %) as 62.5%", () => expect(fmtPct(62.5)).toBe("62.5%"));
  it("formats 0.1 as 10.0%",    () => expect(fmtPct(0.1)).toBe("10.0%"));
});

// ── fmtDelta ──
describe("fmtDelta", () => {
  it("returns — for null",      () => expect(fmtDelta(null)).toBe("—"));
  it("returns — for NaN",       () => expect(fmtDelta(NaN)).toBe("—"));
  it("returns — for 0",         () => expect(fmtDelta(0)).toBe("—"));
  it("formats positive as +N",  () => expect(fmtDelta(234)).toBe("+234"));
  it("formats negative as −N",  () => expect(fmtDelta(-12)).toBe("−12"));
  it("formats large positive",  () => expect(fmtDelta(1500)).toBe("+1.5K"));
  it("formats large negative",  () => expect(fmtDelta(-2_000_000)).toBe("−2.0M"));
});

// ── fmtRelativeTime ──
describe("fmtRelativeTime", () => {
  it("returns — for null",      () => expect(fmtRelativeTime(null)).toBe("—"));
  it("returns — for undefined", () => expect(fmtRelativeTime(undefined)).toBe("—"));
  it("returns — for empty",    () => expect(fmtRelativeTime("")).toBe("—"));
  it("returns just now for <30s ago", () => {
    const iso = new Date(Date.now() - 10_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("just now");
  });
  it("returns 1 minute ago for ~60s", () => {
    const iso = new Date(Date.now() - 65_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("1 minute ago");
  });
  it("returns N minutes ago for ~10min", () => {
    const iso = new Date(Date.now() - 10 * 60_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("10 minutes ago");
  });
  it("returns 1 hour ago for ~1hr", () => {
    const iso = new Date(Date.now() - 70 * 60_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("1 hour ago");
  });
  it("returns N hours ago for ~5hrs", () => {
    const iso = new Date(Date.now() - 5 * 3600_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("5 hours ago");
  });
  it("returns 1 day ago", () => {
    const iso = new Date(Date.now() - 25 * 3600_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("1 day ago");
  });
  it("returns N days ago", () => {
    const iso = new Date(Date.now() - 3 * 86400_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("3 days ago");
  });
  it("returns just now for future timestamps", () => {
    const iso = new Date(Date.now() + 5_000).toISOString();
    expect(fmtRelativeTime(iso)).toBe("just now");
  });
});

// ── fmtDuration ──
describe("fmtDuration", () => {
  it("returns — for null",        () => expect(fmtDuration(null)).toBe("—"));
  it("returns — for negative",    () => expect(fmtDuration(-1)).toBe("—"));
  it("formats 0s",                () => expect(fmtDuration(0)).toBe("0s"));
  it("formats 45s",               () => expect(fmtDuration(45)).toBe("45s"));
  it("formats 90s as 1m 30s",     () => expect(fmtDuration(90)).toBe("1m 30s"));
  it("formats 7200s as 120m 0s",  () => expect(fmtDuration(7200)).toBe("120m 0s"));
});
