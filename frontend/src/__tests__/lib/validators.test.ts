import {
  TierThresholdFieldsSchema,
  TierThresholdsSchema,
  ConfigRowSchema,
  parseThresholdValue,
} from "@/lib/validators";

// ── TierThresholdFieldsSchema ──
describe("TierThresholdFieldsSchema", () => {
  const valid = {
    min_volume:     100,
    min_last_price: 0.5,
    min_oi:         500,
    atm_pct:        0.10,
    max_dte:        45,
  };

  it("accepts valid fields", () => {
    expect(TierThresholdFieldsSchema.safeParse(valid).success).toBe(true);
  });

  it("accepts 0 for all fields", () => {
    const zero = { min_volume: 0, min_last_price: 0, min_oi: 0, atm_pct: 0, max_dte: 0 };
    expect(TierThresholdFieldsSchema.safeParse(zero).success).toBe(true);
  });

  it("rejects negative min_volume", () => {
    expect(TierThresholdFieldsSchema.safeParse({ ...valid, min_volume: -1 }).success).toBe(false);
  });

  it("rejects atm_pct > 1", () => {
    expect(TierThresholdFieldsSchema.safeParse({ ...valid, atm_pct: 1.1 }).success).toBe(false);
  });

  it("accepts atm_pct exactly 1.0", () => {
    expect(TierThresholdFieldsSchema.safeParse({ ...valid, atm_pct: 1.0 }).success).toBe(true);
  });

  it("rejects fractional max_dte", () => {
    expect(TierThresholdFieldsSchema.safeParse({ ...valid, max_dte: 45.5 }).success).toBe(false);
  });

  it("rejects negative max_dte", () => {
    expect(TierThresholdFieldsSchema.safeParse({ ...valid, max_dte: -1 }).success).toBe(false);
  });
});

// ── TierThresholdsSchema ──
describe("TierThresholdsSchema", () => {
  const valid = {
    t1_min_volume: 100, t1_min_last_price: 0.5, t1_min_oi: 500, t1_atm_pct: 0.10, t1_max_dte: 30,
    t2_min_volume:  50, t2_min_last_price: 0.3, t2_min_oi: 200, t2_atm_pct: 0.15, t2_max_dte: 45,
    t3_min_volume:  20, t3_min_last_price: 0.2, t3_min_oi: 100, t3_atm_pct: 0.20, t3_max_dte: 60,
  };

  it("accepts a complete valid row", () => {
    expect(TierThresholdsSchema.safeParse(valid).success).toBe(true);
  });

  it("rejects invalid t2_atm_pct", () => {
    expect(TierThresholdsSchema.safeParse({ ...valid, t2_atm_pct: 2.0 }).success).toBe(false);
  });

  it("rejects missing field", () => {
    const { t3_min_oi, ...rest } = valid;
    void t3_min_oi;
    expect(TierThresholdsSchema.safeParse(rest).success).toBe(false);
  });
});

// ── ConfigRowSchema ──
describe("ConfigRowSchema", () => {
  it("accepts valid key/value", () => {
    expect(ConfigRowSchema.safeParse({ key: "max_symbols", value: "500" }).success).toBe(true);
  });

  it("accepts empty value string", () => {
    expect(ConfigRowSchema.safeParse({ key: "flag", value: "" }).success).toBe(true);
  });

  it("rejects empty key", () => {
    expect(ConfigRowSchema.safeParse({ key: "", value: "123" }).success).toBe(false);
  });

  it("rejects missing key", () => {
    expect(ConfigRowSchema.safeParse({ value: "123" }).success).toBe(false);
  });
});

// ── parseThresholdValue ──
describe("parseThresholdValue", () => {
  it("parses a valid number string", () => {
    const result = parseThresholdValue("100", "min_volume");
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value).toBe(100);
  });

  it("returns error for empty string", () => {
    const result = parseThresholdValue("", "min_volume");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toBe("Must be a number");
  });

  it("returns error for non-numeric string", () => {
    const result = parseThresholdValue("abc", "min_volume");
    expect(result.ok).toBe(false);
  });

  it("returns error when atm_pct > 1", () => {
    const result = parseThresholdValue("1.5", "atm_pct");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/1.0/);
  });

  it("returns error when max_dte is fractional", () => {
    const result = parseThresholdValue("45.5", "max_dte");
    expect(result.ok).toBe(false);
  });

  it("returns error for negative min_oi", () => {
    const result = parseThresholdValue("-5", "min_oi");
    expect(result.ok).toBe(false);
  });

  it("accepts 0 for min_volume", () => {
    const result = parseThresholdValue("0", "min_volume");
    expect(result.ok).toBe(true);
  });

  it("trims whitespace before parsing", () => {
    const result = parseThresholdValue("  50  ", "min_volume");
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value).toBe(50);
  });
});
