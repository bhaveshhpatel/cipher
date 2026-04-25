# Feature 4A-OI: Avg Chain OI Pipeline

**Shipped:** 2026-04-25  
**Branch:** `main`  
**Chunks:** 1A – 1D (implementation) + 2A – 2C (tests) + 3 (migration docs) + 4 (changelog + this doc)

---

## Motivation

Before this feature, `tier_engine._classify()` contained an OI **grace path**: if
`open_interest == 0` (which happened on every cold start because OI was never populated),
the symbol would still be promoted to T1 or T2 based on volume and price alone.

This meant:
- The `open_interest` column added by migration 010 was always `NULL` in production.
- T1/T2 classification was effectively OI-blind.
- The `tier_thresholds.t*_min_oi` admin fields had no effect.

4A-OI fixes all three by computing real avg chain OI during registry build and wiring
it into tier classification before any DB writes.

---

## Architecture

### New data flow (cold start / tradier_validated path)

```
lifespan() startup
├── _resolve_startup_universe()
│     ├── load_universe() → quotes (open_interest=0)
│     └── assign_tiers(quotes)  → preliminary tier_map  [Pass 1, OI=0]
├── init_registry(watchlist, tier_map)
├── await registry.build()           ← computes _oi_by_ticker per symbol
├── oi_map = registry.get_oi_map()   ← {symbol: avg_chain_oi}
├── _stamp_oi(quotes, oi_map)         ← sets quote.open_interest in-place
├── tier_map = assign_tiers(quotes)   ← OI-informed classification  [Pass 2]
├── registry.set_tier_map(tier_map)
└── upsert_symbol_quotes(quotes, tier_map)  ← writes open_interest + tier to DB
```

### Warm start (DB snapshot path)

When a fresh DB snapshot is available, `_resolve_startup_universe()` returns
`quotes=[]`, skipping the OI two-pass entirely. The stored `tier_map` already
reflects the last cold-start OI-aware classification.

---

## Files changed

| Chunk | File | Change summary |
|-------|------|----------------|
| 1A | `services/symbol_registry.py` | Added `_oi_by_ticker: dict[str, int]` attribute; populate it during `_build_ticker()` as the average of loaded contract OI values; expose via `get_oi_map() → dict[str, int]` (returns a shallow copy for mutation safety) |
| 1B | `services/tier_engine.py` | Removed `if oi == 0: # grace path` block from `_classify()`; all 3 conditions (vol ≥ threshold, price ≥ threshold, oi ≥ threshold) now hard-required for T1 and T2 |
| 1C | `services/universe_store.py` | Added `"open_interest": quote.open_interest` to the row dict in `_sync_upsert_symbol_quotes()`; was previously omitted, leaving the column NULL |
| 1D | `backend/main.py` | Added `_stamp_oi(quotes, oi_map)` helper; inserted the two-pass OI re-tiering block in `lifespan()` and the same in `_universe_refresh_loop()` |

---

## OI grace-path removal rationale

The grace path (`if oi == 0: promote anyway`) was added as a bootstrap workaround
when OI data wasn't available at classification time. With 4A-OI, real OI is always
available before classification runs, so the workaround is no longer needed.

Removing it means:
1. `tier_thresholds.t*_min_oi` thresholds are now enforced in production.
2. Symbols that went live with no contracts (e.g. recently listed tickers) correctly
   land in T3 until their chain is populated.
3. Tests TE-23/TE-24 will fire immediately if the grace path is ever re-introduced.

---

## Test map

| Test file | Class | Test IDs | What’s covered |
|-----------|-------|----------|----------------|
| `test_4a_oi_pipeline.py` | `TestSymbolRegistryOiMap` | — | `get_oi_map()` empty, populated, zero, copy safety |
| `test_4a_oi_pipeline.py` | `TestClassifyNoGrace` | — | T1/T2 all-3 conditions; oi=0 → T3; boundary; floor |
| `test_4a_oi_pipeline.py` | `TestStampOi` | — | Correct stamp; missing ticker → 0; in-place; edge cases |
| `test_4a_oi_pipeline.py` | `TestOiDrivenTierIntegration` | — | End-to-end two-pass; mixed tiers; preliminary vs final diff |
| `test_4a_tier_engine.py` | `TestOiGracePathRemoved` | TE-23–26 | Grace revert detection; T1 exact boundary; off-by-one |
| `test_universe_store.py` | `TestUpsertSymbolQuotesOi` | US-OI-01–04 | `open_interest` key present; value correct; None→NULL; coexists with tier |

**Total new tests:** 28

---

## Rollback procedure

The `open_interest` column (migration 010) does not need to be rolled back — it is
NULLable and has always existed.

To roll back the 4A-OI application changes:

1. **Revert Chunk 1B** (`tier_engine._classify`): re-add the OI grace path.
   - Tests TE-23 and TE-24 will immediately fail, confirming the revert.
2. **Revert Chunk 1C** (`universe_store._sync_upsert_symbol_quotes`): remove the
   `open_interest` key from the row dict.
   - Test US-OI-01 will fail.
3. **Revert Chunk 1D** (`main.py`): remove `_stamp_oi()` and the two-pass block.
4. **Revert Chunk 1A** (`symbol_registry`): remove `_oi_by_ticker` and `get_oi_map()`.

The DB `open_interest` column will revert to always-NULL but no data is lost.

---

## FAQ

**Q: What OI value does `_oi_by_ticker` store?**  
A: The average of `open_interest` across all loaded contracts for the symbol’s chain
(all strikes × expiries within the tier’s ATM/DTE window).

**Q: What if a symbol has contracts loaded but all OI values are 0?**  
A: `avg == 0`, so `oi_map[symbol] == 0`, the stamp sets `quote.open_interest = 0`,
and `_classify()` returns T3. This is correct — a chain with zero open interest is
not liquid enough for T1/T2.

**Q: Does the warm-start path (DB snapshot) ever re-run OI tiering?**  
A: Not at startup — it uses the persisted `tier_map`. The next cold start or 24h
background refresh will re-run the two-pass pipeline and update both the tier and
open_interest columns in the DB.

**Q: Is there a performance impact from blocking startup on `registry.build()`?**  
A: `registry.build()` was already called unconditionally. The only new cost is
`_stamp_oi()` (O(n) loop over quotes) and a second `assign_tiers()` call, both
negligible (≤50ms for a 1 500-symbol universe).
