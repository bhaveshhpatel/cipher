/**
 * gateUtils.ts — Pure utility functions for gate value formatting and conversion
 * ADMIN-UI-001
 */

import { MS_GATES, DOLLAR_GATES } from '@/types/gates';

/**
 * Converts a raw API value to a display string shown in the UI.
 * - ms gates: divide by 1000 → show seconds
 * - dollar gates: comma-formatted integer (no decimals)
 * - dte_floor_multiplier: one decimal
 * - everything else: as-is
 */
export function formatGateValue(gateName: string, rawValue: number): string {
  if (MS_GATES.has(gateName)) {
    return String(rawValue / 1000);
  }
  if (DOLLAR_GATES.has(gateName)) {
    return String(Math.round(rawValue));
  }
  if (gateName === 'dte_floor_multiplier') {
    return String(rawValue);
  }
  return String(rawValue);
}

/**
 * Converts a user-entered display string back to the raw API value.
 * - ms gates: multiply by 1000
 * - everything else: parse as float
 * Returns NaN if not parseable.
 */
export function parseGateInput(gateName: string, displayValue: string): number {
  const num = parseFloat(displayValue);
  if (isNaN(num)) return NaN;
  if (MS_GATES.has(gateName)) {
    return num * 1000;
  }
  return num;
}

/**
 * Returns a formatted display suffix/prefix label for a gate (for helper text).
 * e.g. formatRangeText('min_premium', 1000, 500000) → '$1,000 – $500,000'
 */
export function formatRangeText(gateName: string, minValue: number, maxValue: number): string {
  const fmt = (v: number) => {
    if (MS_GATES.has(gateName)) return `${v / 1000}s`;
    if (DOLLAR_GATES.has(gateName)) return `$${Math.round(v).toLocaleString()}`;
    if (gateName === 'dte_floor_multiplier') return `${v}×`;
    return String(v);
  };
  return `Range: ${fmt(minValue)} – ${fmt(maxValue)}`;
}

/**
 * Returns the display-unit min/max for an input element.
 * For ms gates, divide by 1000 for the displayed input bounds.
 */
export function getInputBounds(
  gateName: string,
  minValue: number,
  maxValue: number,
): { min: number; max: number } {
  if (MS_GATES.has(gateName)) {
    return { min: minValue / 1000, max: maxValue / 1000 };
  }
  return { min: minValue, max: maxValue };
}
