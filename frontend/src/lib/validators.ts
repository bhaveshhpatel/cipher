import { z } from "zod";

/**
 * Zod schema for a single tier’s threshold fields.
 * All numeric fields must be non-negative.
 * atm_pct must be ≤1.0 (it’s a decimal fraction, e.g. 0.10 = 10%).
 * max_dte must be a non-negative integer.
 */
export const TierThresholdFieldsSchema = z.object({
  min_volume:     z.number().nonnegative("Must be ≥ 0"),
  min_last_price: z.number().nonnegative("Must be ≥ 0"),
  min_oi:         z.number().nonnegative("Must be ≥ 0"),
  atm_pct:        z.number().min(0, "Must be ≥ 0").max(1, "Must be ≤ 1.0 (decimal)"),
  max_dte:        z.number().int("Must be a whole number").nonnegative("Must be ≥ 0"),
});

export type TierThresholdFields = z.infer<typeof TierThresholdFieldsSchema>;

/**
 * Schema for the full tier thresholds row (all 3 tiers together).
 * Used to validate the entire PATCH payload.
 */
export const TierThresholdsSchema = z.object({
  t1_min_volume:      z.number().nonnegative(),
  t1_min_last_price:  z.number().nonnegative(),
  t1_min_oi:          z.number().nonnegative(),
  t1_atm_pct:         z.number().min(0).max(1),
  t1_max_dte:         z.number().int().nonnegative(),
  t2_min_volume:      z.number().nonnegative(),
  t2_min_last_price:  z.number().nonnegative(),
  t2_min_oi:          z.number().nonnegative(),
  t2_atm_pct:         z.number().min(0).max(1),
  t2_max_dte:         z.number().int().nonnegative(),
  t3_min_volume:      z.number().nonnegative(),
  t3_min_last_price:  z.number().nonnegative(),
  t3_min_oi:          z.number().nonnegative(),
  t3_atm_pct:         z.number().min(0).max(1),
  t3_max_dte:         z.number().int().nonnegative(),
});

export type TierThresholds = z.infer<typeof TierThresholdsSchema>;

/**
 * Schema for a single ingestion config row update.
 * key must be non-empty; value is kept as string (backend casts).
 */
export const ConfigRowSchema = z.object({
  key:   z.string().min(1, "Key cannot be empty"),
  value: z.string(),
});

export type ConfigRow = z.infer<typeof ConfigRowSchema>;

/**
 * Parse a raw string value as a number for tier threshold fields.
 * Returns { ok: true, value } or { ok: false, error }.
 */
export function parseThresholdValue(
  raw: string,
  field: keyof TierThresholdFields,
): { ok: true; value: number } | { ok: false; error: string } {
  const n = Number(raw.trim());
  if (raw.trim() === "" || isNaN(n)) {
    return { ok: false, error: "Must be a number" };
  }
  const partial = { [field]: n } as Partial<TierThresholdFields>;
  const result = TierThresholdFieldsSchema.partial().safeParse(partial);
  if (!result.success) {
    return { ok: false, error: result.error.errors[0]?.message ?? "Invalid value" };
  }
  return { ok: true, value: n };
}
