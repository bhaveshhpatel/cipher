"""
services/symbol_registry.py — Layer 1: OCC Symbol Registry

FIX P3 (2026-04-27): _build_ticker now uses get_option_chain_bulk() instead
  of get_option_chain() so build() uses _BULK_CHAIN_SEM(10) rather than the
  live-stream _CHAIN_SEM(2). Cold-start chain throughput increases ~5x.

FIX P4 (2026-04-27): build() now performs an incremental warm-restart when
  the registry was pre-seeded from DB (load_from_db returned > 0 rows). Only
  tickers whose minimum DTE in the seeded registry is 0 (contracts expired
  today) are re-fetched; all other tickers are carried forward unchanged.
  Warm-restart chain API calls drop from ~17,360 to ~50-400.

FIX C-3 (2026-04-27): Post-build OI-based tier reclassification.
  assign_tiers() signature: async def assign_tiers(quotes: list[SymbolQuote], ...)
  The post-build call was: await assign_tiers(oi_map=new_oi_by_ticker, require_oi=True)
  This raised TypeError: assign_tiers() got an unexpected keyword argument 'oi_map',
  which suppressed all post-build OI tier upgrades (all contracts stayed at
  volume-only T3 instead of being upgraded to OI-confirmed T1/T2).
  Fix: inline OI re-tier — classify each ticker in new_oi_by_ticker as T1/T2/T3
  by comparing average OI to thresh t1_min_oi / t2_min_oi thresholds, stamp
  the result onto every ContractMeta in new_registry, and update _tier_map.
  assign_tiers() import retained (still used by _post_build_upsert in main.py).

FIX H1 (2026-04-27): build() now returns a tuple[int, dict[str, dict]]
  (count, raw_quotes). Callers that only need the count ignore the second
  element; _post_build_upsert passes raw_quotes to
  _post_build_upsert so it can skip the duplicate _fetch_batch_quotes call.

FIX H3 (2026-04-27): Removed _seeded_from_db flag entirely. The incremental
  build guard is now `if self._registry:` - the populated registry itself is
  the correct signal for an incremental refresh. This means scheduled
  refresh_loop() calls also get incremental DTE-based pruning instead of
  always doing a full rebuild after the first build()`.\\n  Module-level imports of get_config, _fetch_thresholds, assign_tiers, and
  load_chain are now at the top of the file so unittest.mock.patch targets
  work correctly (patch('services.symbol_registry.get_config') etc.).

FIX M-1 (2026-04-28): Replaced is_ready() len-check with a dedicated
  _build_complete flag set only at the very end of build(). The stream now
  waits until build() has fully finished rather than unblocking the moment
  the first DB-seeded contract appears in the registry, which caused the
  warm-start worker count mismatch (37 vs the expected 45).

FIX M-2 (2026-04-28): is_ready() now returns self._build_complete instead
  of len(self._registry) > 0. load_from_db() does NOT set _build_complete;
  build() sets it at the very end (inside the lock, after self._registry is
  swapped). This guarantees stream workers only spawn against a fully-built,
  fresh-Tradier registry - never a partially-seeded DB snapshot.

FIX M-3 (2026-04-28): _post_build_upsert is split into two separately
  guarded phases. assign_tiers() failure is caught and re-raised so
  upsert_symbol_quotes() is skipped (was silently swallowed). A dedicated
  error counter and warning log make the failure visible without taking down
  the process. The outer non-fatal wrapper in main.py still protects the
  background task but now sees the raised exception.

FIX B-ZERO-PRICE (2026-04-29): When _fetch_stock_prices() returns 0 prices,
  build() previously filtered every ticker out of the _build_with_sem tasks
  (the `if ticker in prices and prices[ticker] > 0` guard silently dropped
  all work) and completed with 0 OCC contracts. New behaviour:
  - If ALL prices are missing: log at ERROR, set zero_price_fallback=True so
    chain fetches still run with ATM filtering bypassed entirely.
  - If SOME prices are missing (partial fetch): tickers with no price fall
    back to bypass mode inside _build_ticker (WARNING per ticker).
  - _build_ticker guard updated: stock_price <= 0 + zero_price_fallback=True
    bypasses the ATM filter (atm_low=0, atm_high=inf) rather than returning.
    DTE gating via tier params still applies normally.

FIX ING-010 (2026-05-07): Add influence_tier_int() as the sole tier accessor.
  _resolve_min_premium() in tradier_stream.py calls influence_tier_int(ticker)
  directly to get the integer tier (1/2/3) and passes it straight to
  gate_config_store.get("min_premium", tier_int). No string intermediary.
  Fallback: 3 (most conservative / T3 defaults) for unknown tickers.

  NOTE: The former influence_tier_string() method and _INT_TIER_TO_STRING dict
  have been removed (ING-012). The int->string->int round-trip they introduced
  was pure overhead — influence_tier_int() already returns the int directly.
  episode_influence_tier() in composite_signal_engine.py is a separate,
  orthogonal function that classifies episode premium size (WHALE/INSTITUTIONAL/
  LARGE/RETAIL) and is unrelated to symbol tier; it is untouched.

ING-010-EPOCH (2026-05-07): Add epoch versioning to SymbolRegistry.
  self.epoch: int is initialised to 0 in __init__ and incremented inside the
  build() lock immediately after self._build_complete = True.
  Contract (mirrors GateConfigStore.epoch):
    - epoch == 0  -> registry has never completed a full build().
    - epoch > 0   -> at least one build() has completed; value is the build
                    generation count (1, 2, 3, ...).
  Consumers (stream_worker, tradier_stream) can watch registry.epoch to
  detect tier-map refreshes without polling individual symbol keys.
  load_from_db() does NOT increment epoch — only build() does, so callers
  can rely on epoch > 0 as a "fully built from Tradier" signal (same
  semantics as _build_complete).

FIX QQ1-A (2026-05-09): Use real tier_params on cold-start build — remove
  bootstrap_params.
  bootstrap_params collapsed all tiers to T3 params ({1: T3, 2: T3, 3: T3})
  during the first build(), meaning T1 tickers (NVDA, AAPL, TSLA etc.) had
  their chains fetched using T3's narrow atm_pct=0.10 / max_dte=30 instead
  of T1's atm_pct=0.20 / max_dte=90. Any institutional contract outside
  that window (e.g. 45-DTE NVDA CALL at 115% moneyness) was silently absent
  from the registry. At stream time, lookup() returned None for these OCC
  symbols and the trade was dropped before accumulator, persist, and signal.
  Fix: pass tier_params directly to _build_ticker() on every call, including
  cold-start. The OI gate is already independently controlled per-tier via
  params.min_oi (baked into _build_tier_params from global_min_oi + thresh
  t{n}_min_oi). bootstrap_params variable removed entirely.
  SA/PBE impact: T1 institutional prints on contracts outside the former T3
  window now register and flow through accumulator + persist from epoch 1.

FIX QQ1-B (2026-05-09): Round OI average instead of integer floor division.
  _build_ticker() and load_from_db() both used `total_oi // count` (integer
  floor division) to compute the per-ticker average OI written to
  _oi_by_ticker. For borderline tickers whose true average sits just above
  t1_min_oi=1000 or t2_min_oi=500, truncation silently mis-classified them
  one tier lower (T2 instead of T1, or T3 instead of T2), applying a higher
  min_premium gate floor to all their flow events.
  Fix: round(total_oi / count) in both sites.

FIX BUILD-HANG (2026-05-12): build() could hang indefinitely when Tradier's
  quote or chain API stalled at the TCP layer before the httpx read timeout
  fired. Both network phases inside build() now have hard asyncio.wait_for()
  deadlines:

  - _fetch_stock_prices(): 45s timeout. On expiry, logs ERROR and sets
    zero_price_fallback=True so chain fetches still run with ATM filtering
    bypassed (existing B-ZERO-PRICE path). _build_complete is guaranteed to
    be set.

  - asyncio.gather(*tasks) for chain fetches: 18000s timeout (covers full
    3949-ticker universe at concurrency=10 with per-request 45s timeouts;
    raised from 1800s which could fire when Tradier stalls pile up across
    3850+ queued tickers). On expiry, logs ERROR and proceeds with whatever
    contracts were fetched before the deadline; _build_complete is still set
    so stream workers can spawn against the partial registry.

  Both timeouts are wrapped in try/except asyncio.TimeoutError so the
  outer non-fatal wrapper in main.py/_background_build_and_upsert is not
  triggered — the build completes (possibly partial) rather than raising.

FIX BUILD-HANG-PER-REQUEST (2026-05-14): Each individual get_option_chain_bulk()
  call inside _build_ticker() is now wrapped in asyncio.wait_for(timeout=45s).
  Without this, a single stalled TCP connection held a semaphore slot for the
  entire gather window. With concurrency=10, a handful of stalled connections
  reduced effective throughput to near zero.

  With per-request timeouts:
  - Each slot is freed within 45s max regardless of Tradier TCP behaviour.
  - Semaphore stays productive at full concurrency=10 throughout.
  - Expected build time: 5-8 min on a clean day, ~10 min on degraded days.
  - BUILD-HANG gather timeout (18000s) is a true last-resort safety net
    that should never fire under normal or degraded operating conditions.

FIX POOL-MISMATCH (2026-05-14): max_connections raised to 75 (for sem=50).
  CONCURRENCY-10 (2026-05-14): reverted to max_connections=30 (3x sem=10).

FIX SHUTDOWN-CANCEL (2026-05-12): _build_with_sem now catches CancelledError
  and re-raises immediately instead of letting it propagate through
  `async with sem:` as an unhandled future exception.

LOG-CHAIN (2026-05-14): Chain-pull progress, per-request timeout, and
  elapsed-time logging added to build() and _build_ticker().

  - _build_with_sem: shared atomic counter logs progress every 250 tickers
    (and at 100% completion) showing count/total/%, contracts accumulated so
    far, and elapsed seconds since chain gather started. Gives real-time
    visibility into cold-start chain pull with zero logic changes.

  - _build_ticker: asyncio.TimeoutError on get_option_chain_bulk() now logs
    at WARNING with ticker + expiry string so stalling tickers are
    identifiable (was silently `continue`-ing with no trace).

  - build(): chain gather phase timed with time.monotonic(). Elapsed seconds
    logged on both clean completion and gather-timeout path.

LOG-CHAIN-V2 (2026-05-14): Granular per-ticker and per-expiry logging.

  - Per-ticker START: logs ticker + tier when semaphore slot is acquired so
    each of the 10 concurrent slots is visible in real time.

  - Per-ticker DONE: logs ticker + elapsed ms + contracts found for that
    ticker immediately after _build_ticker returns.

  - Per-expiry inside _build_ticker: each expiry fetched logs contract count
    so dead expiries (0 contracts) and productive ones are distinguishable.

  - ETA in progress line: every _CHAIN_PROGRESS_INTERVAL tickers the
    progress log includes estimated seconds to completion based on current
    rate (contracts/s).

  - Slot starvation warning: _CHAIN_STALL_WARN_S=10s inner deadline logs
    WARNING with ticker name if a chain fetch exceeds 10s but has not yet
    hit the 45s hard timeout. Implemented via a two-stage wait_for cascade
    in _build_ticker so the full 45s budget is preserved.

FLUSH-PERIODIC (2026-05-14): Flush partial registry to DB every
  _CHAIN_FLUSH_INTERVAL=500 tickers during the gather phase instead of
  waiting until the full build is complete.

  - A background asyncio.Task runs _periodic_flush() alongside the gather.
  - _periodic_flush() wakes every _CHAIN_FLUSH_INTERVAL_S=30s, snapshots
    the current new_registry dict, and calls save_chain() with whatever
    contracts have been fetched so far.
  - The final full save_chain() call in _persist_to_db() is preserved as
    the authoritative complete write; periodic flushes are best-effort
    (errors are logged as WARNING, never raised).
  - On Render cold-start this means contracts start appearing in DB within
    ~30s of build() starting rather than only after the full ~60-120s
    completes. Warm restarts benefit too since the DB snapshot is more
    current if a process is killed mid-build.

CHAIN-ALL (2026-05-14): Switch _build_ticker to single all-expiry call.
  Previously: get_expirations() (call 1) + get_option_chain_bulk() per expiry
  (calls 2..N). For a ticker with 4.5 avg expiries that is 5.5 calls; across
  3,900 tickers = ~21,450 total API calls.

  Now: get_option_chain_bulk_all() (1 call, no expiration param). Tradier
  returns all expiries in one response; each contract carries expiration_date.
  Client-side grouping by expiration_date, then DTE / ATM / OI / type filters
  applied identically to the per-expiry loop. 3,900 total API calls.

  - 82% fewer calls: ~21,450 -> ~3,900
  - ~66-76% faster build: ~343s clean -> ~117s; ~1,287s degraded -> ~312s
  - HTTP 400 terminates the whole ticker (correct: 400 = no listed options)
  - DTE filter moves from pre-fetch to post-fetch (client-side) — no
    behaviour change, minor extra memory per in-flight response (~180KB peak)
  - get_expirations() import removed from symbol_registry; get_option_chain_bulk
    import also removed (still used by chain_store refresh worker via
    tradier_stream path — not symbol_registry).
  - Two-stage stall warning (10s/45s) carried over unchanged.

REVERT-CHAIN-ALL (2026-05-15): Reverted _build_ticker from CHAIN-ALL back to
  get_expirations() + per-expiry get_option_chain_bulk() loop.
  Root cause: Tradier does NOT stamp expiration_date on individual contracts
  when the expiration param is omitted from /v1/markets/options/chains.
  The CHAIN-ALL response returns contracts without that field, so by_expiry
  grouped everything under "" -> date.fromisoformat("") raised ValueError ->
  every contract skipped -> contracts={} for all tickers -> zero DB flushes.
  Imports reverted: get_option_chain_bulk_all removed; get_expirations and
  get_option_chain_bulk restored.

CONCURRENCY-10 (2026-05-14): Lower _DEFAULT_BUILD_CONCURRENCY from 50 -> 10.
  Companion to tradier_client.py CONCURRENCY-10. Reduces Tradier API pressure
  and eliminates rate-limit-induced HTTP 400/429s that were preventing all
  ~3,900 tickers from completing chain fetches. Build time increases modestly
  (~5-8 min clean vs ~2-3 min at concurrency=50) but success rate improves
  from ~130 tickers to full ~3,900 coverage.

FIX-SINGLETON (2026-05-14): Add init_registry() and get_registry() module-level
  singleton functions.
  main.py line 183 imports both names but they were never defined in this module
  — only the SymbolRegistry class existed. This caused an ImportError on every
  uvicorn startup, preventing the backend from launching.
  Fix: add _registry_instance module-level variable plus two functions:
    init_registry(watchlist, tier_map) — creates and stores the singleton.
    get_registry()                     — returns the current singleton or None.

FIX-QUOTES-ITER (2026-05-15): Fix _fetch_stock_prices iterating dict keys
  instead of values after FIX-QUOTES-RESP changed get_quotes_batch() return
  type from list[dict] to dict[str, dict].

  get_quotes_batch() now returns {symbol: quote_dict}. The old loop
  "for q in quotes:" iterates over string keys ("AAPL", "MSFT", ...).
  q.get("symbol") on a string raises:
    'str' object has no attribute 'get'
  Caught by bare except -> logged as WARNING per batch -> all 20 batches fail
  silently -> 0 prices returned -> B-ZERO-PRICE fallback fires on every
  cold-start build (ATM filter bypassed, chain stall warnings flood logs).

  Fix: "for q in quotes.values()" so q is the quote dict as intended.

FIX-INCREMENTAL-REGISTRY (2026-05-15): Populate new_registry in-place inside
  _build_with_sem so FLUSH-PERIODIC and progress logs see live data.

  Root cause: new_registry was only updated in the post-gather loop (after
  asyncio.gather() completed). During the entire 5-10 min gather window,
  new_registry stayed empty/stale, causing two silent failures:

  1. contracts=0 in every chain progress log — total_so_far read
     len(new_registry) which was always 0 during the gather.

  2. FLUSH-PERIODIC never flushed — _periodic_flush() woke every 30s,
     called dict(snap_ref[0]) on the still-empty new_registry, hit the
     `if not snapshot: continue` guard, and logged nothing.

  Fix: move new_registry.update(result) + OI sum accumulation into
  _build_with_sem immediately after _build_ticker returns. new_registry
  is now populated incrementally as each of the 10 concurrent slots finishes.

  Post-gather loop is retained for the OI average recomputation step
  (dividing accumulated sums by contract counts); the redundant
  new_registry.update() call is removed from that loop since the dict
  is already fully populated by the time gather() returns.

FIX-SAVE-CHAIN-ARGS (2026-05-15): Fix swapped arguments in _periodic_flush
  and _persist_to_db.

  save_chain(snapshot_id, registry_dict) — both call sites had the arguments
  reversed: passing the registry dict as the first positional arg (snapshot_id)
  and the snapshot_id string as the second (registry_dict). Inside save_chain,
  `for occ, m in registry_dict.items()` was therefore called on a string,
  raising:
    'str' object has no attribute 'items'
  Caught and logged every 30s as:
    FLUSH-PERIODIC: flush failed — 'str' object has no attribute 'items'
  and silently swallowed every _persist_to_db write, meaning the registry was
  never actually persisted to DB after any build.

  Fix: correct argument order in both call sites:
    _periodic_flush:  save_chain(snapshot_id_str, snapshot_dict)
    _persist_to_db:   save_chain(snapshot_id,    self._registry)

FIX-PARTIAL-UUID (2026-05-15): Fix _periodic_flush passing literal "partial"
  as snapshot_id to save_chain() when _persisted_snapshot_id is None.

  Root cause: `snapshot_id = self._persisted_snapshot_id or "partial"` produced
  the string "partial" on cold-start builds (before any _persist_to_db() call
  sets _persisted_snapshot_id to a real UUID). Supabase/PostgREST rejects any
  non-UUID value for the options_chain_cache.snapshot_id uuid column:
    invalid input syntax for type uuid: "partial"
  Only _periodic_flush() hit this path; _persist_to_db() uses
  self._persisted_snapshot_id directly (always a real UUID when called).

  Fix: branch on self._persisted_snapshot_id in _periodic_flush:
    - If set: use it (periodic flush updates the same snapshot row as persist)
    - If None (cold start): generate uuid4() so every partial flush is a
      valid UUID upsert. The final _persist_to_db() write is still the
      authoritative complete write.

FIX-GATHER-TIMEOUT (2026-05-15): Raise _CHAIN_GATHER_TIMEOUT_S 600 -> 1800.
  The 600s ceiling was sized for concurrency=50. Since CONCURRENCY-10 lowered
  throughput to ~5-8 min clean / ~10-15 min degraded, the 600s wall fired on
  degraded days and cut the gather short, leaving a partial registry. 1800s
  (30 min) is a true last-resort ceiling that never fires under normal or
  degraded operation at concurrency=10.

FIX-LATEST-UUID (2026-05-15): _persist_to_db no longer falls back to the
  literal string "latest" as snapshot_id.

  Root cause: `snapshot_id = self._persisted_snapshot_id or "latest"` passed
  the string "latest" to save_chain() on every cold-start call (before any
  prior _persist_to_db() run had set _persisted_snapshot_id). Supabase/PostgREST
  immediately rejected the upsert into options_universe_snapshots (uuid column):
    invalid input syntax for type uuid: "latest"
  save_chain() returned False; the FK violation blocked all child upserts;
  but _persist_to_db logged success unconditionally — silent data loss.

  Fix: generate a fresh uuid4() when _persisted_snapshot_id is None so the
  upsert is always accepted. Store it in _persisted_snapshot_id so subsequent
  periodic flushes update the same snapshot row.

FIX-SAVE-CHAIN-RETVAL (2026-05-15): _persist_to_db now checks save_chain()'s
  bool return value and logs a WARNING on False.

  Previously save_chain() returned False on any DB error (FK violation,
  network timeout, etc.) but _persist_to_db logged
  "saved N contracts (snapshot_id=...)" unconditionally regardless of the
  return value, creating a silent data-loss path. Now:
    ok = await save_chain(snapshot_id, self._registry)
    if not ok:
        log.warning("[symbol_registry] _persist_to_db: save_chain returned False ...")

FIX-NO-MIDBUILD-FLUSH (2026-05-15): _periodic_flush now skips flushing while
  build is still in progress (Option 2 — stop mid-build flushing entirely).

  Root cause: periodic flushes during the gather phase called save_chain()
  multiple times with a new snapshot UUID each wake (or the same UUID if
  _persisted_snapshot_id was already set), creating 3-5+ partial snapshot
  rows in options_universe_snapshots — each with a different epoch and
  contract count — none of which matched the final in-memory state. This
  caused DB count vs log count mismatches and orphaned partial rows that
  were never cleaned up.

  Fix: guard the flush body with `if not self._build_complete: continue`.
  _build_complete is set to True inside the build() lock after self._registry
  is swapped and before _persist_to_db() is called. This means:
    - During gather (build running): _periodic_flush wakes, sees
      _build_complete=False, skips, goes back to sleep. Zero partial writes.
    - After build() completes: _persist_to_db() does the single authoritative
      write. If refresh_loop() triggers a second build(), _build_complete is
      reset to False inside the lock at the start of the new build, so the
      guard correctly suppresses mid-refresh flushes too.
  The flush task is still created and cancelled as before; its presence is
  harmless. Only the flush body changes.

FIX-GATHER-TIMEOUT-18000 (2026-05-15): Raise _CHAIN_GATHER_TIMEOUT_S 1800 -> 18000.
  At concurrency=10 with Tradier stalls consuming the full 45s-per-request
  budget, 3849 queued tickers could exhaust the 1800s ceiling before gather
  completed, leaving a partial registry and immediately queuing 3850 tickers
  for incremental follow-up. 18000s (5 hours) is a true last-resort ceiling
  that should never fire under normal or degraded operating conditions.

FIX-SYNTAX-LINE690 (2026-05-15): Close truncated log.error() call at line 690.
  The prior commit wrote the file with the BUILD-HANG _fetch_stock_prices
  timeout handler log.error() call cut off mid-string — the closing `)` and
  the rest of the except block were missing. Python's parser raised:
    SyntaxError: '(' was never closed
  at line 690, preventing uvicorn from importing main.py and crashing the
  backend on every deploy. Fix: restore the complete except asyncio.TimeoutError
  block and the remainder of the build() method body.

FIX C-3 REWRITE (2026-05-17): Replace broken assign_tiers(oi_map=...) call
  with inline OI re-tier inside build().
  assign_tiers() signature: async def assign_tiers(quotes: list[SymbolQuote], ...)
  The post-build call was: await assign_tiers(oi_map=new_oi_by_ticker, require_oi=True)
  This raised TypeError: assign_tiers() got an unexpected keyword argument 'oi_map',
  which suppressed all post-build OI tier upgrades (all contracts stayed at
  volume-only T3 instead of being upgraded to OI-confirmed T1/T2).
  Fix: inline OI re-tier — classify each ticker in new_oi_by_ticker as T1/T2/T3
  by comparing average OI to thresh t1_min_oi / t2_min_oi thresholds, stamp
  the result onto every ContractMeta in new_registry, and update _tier_map.
  assign_tiers() import retained (still used by _post_build_upsert in main.py).

FIX-DEAD-TICKER (2026-05-19): Track consecutive get_expirations() empty returns
  (HTTP 400 = no listed options) per symbol. After _DEAD_TICKER_THRESHOLD=3
  consecutive empty returns, the symbol is added to _dead_ticker_set and
  future build() calls skip it entirely — zero API calls, zero semaphore time.
  This prevents the BBAI/BILI/BHVN/BCRX/BIO/BLFS 400-storm from consuming
  Tradier quota that should go to live T1 names (META, MSFT, BAC etc.).

  Details:
  - _dead_ticker_strikes: dict[str, int] — consecutive empty-expiry count per ticker
  - _dead_ticker_set: set[str] — confirmed dead; skipped in _build_with_sem
  - Strikes reset to 0 on any successful non-empty get_expirations() response
  - _dead_ticker_set persists across refresh_loop() rebuilds (compounds over time)
  - Cleared only on process restart (clean slate on deploy)
  - Summary log at end of each build: dead count, skipped count, quota saved

DTE-FILTER-LOG (2026-05-19): Log expiries dropped by dte > params.max_dte guard
  inside _build_ticker. At DEBUG level for T2/T3; at INFO level when a T1 ticker
  has expiries dropped (max_dte=90 for T1 — should be rare and flags a potential
  DTE ceiling misconfiguration in ingestion_config).

FIX-REFRESH-TIER-PREBUILD (2026-05-19): Pre-classify _tier_map inside build()
  before _build_with_sem tasks are dispatched.

  refresh_loop() calls build() directly, bypassing the pre-fetch + assign_tiers()
  sequence that main.py runs before calling build() on startup. This means every
  hourly chain pull used whatever _tier_map was last set at startup — so any symbol
  that shifted tiers intraday (e.g. a T3 spiked into T1 OI territory) was fetched
  with the wrong atm_pct / max_dte window for the entire remainder of the session.

  The post-build OI re-tier (FIX C-3) reclassifies after the gather, but that is
  too late: the wrong fetch window was already used to pull the chain.

  Fix: inside build(), immediately after self._stock_prices is set and before
  _build_with_sem tasks are created, iterate self._oi_by_ticker and classify each
  symbol as T1/T2/T3 using the t1_min_oi / t2_min_oi thresholds already read from
  thresh at the top of build(). Update self._tier_map in-place. Zero extra API calls.

  Coverage after this fix:
    startup path    — covered by FIX-QQ1-BUILD-SEQUENCING in main.py (pre-fetch)
    prewarm path    — covered by _registry_prewarm_loop's own pre-fetch
    refresh_loop()  — NOW covered by this pre-classify inside build()

FIX-TIER-DEFAULTS (2026-05-19): _build_tier_params() hardcoded fallback values
  were wrong and internally swapped relative to live DB tier_thresholds values:
    T1: atm_pct was 0.20 (DB=0.15), max_dte was 90 (DB=60)
    T2: atm_pct was 0.15 (DB=0.20), max_dte was 60 (DB=90)
    T3: atm_pct was 0.10 (DB=0.20), max_dte was 30 (DB=90)
  These fallbacks only fire when thresh.get() returns nothing (i.e. when
  _fetch_thresholds() itself falls back to _DEFAULT_THRESHOLDS) — and since
  _DEFAULT_THRESHOLDS now mirrors the DB exactly, the blast radius was low.
  Fixed all three tiers to match DB values and _DEFAULT_THRESHOLDS.

FIX-T3-MIN-OI-PRECLASSIFY (2026-05-19): FIX-REFRESH-TIER-PREBUILD pre-classify
  block read t1_min_oi and t2_min_oi from thresh but the else branch (tier=3
  assignment) had no t3_min_oi floor guard. Symbols with avg OI below
  t3_min_oi=100 were classified T3 and dispatched to _build_with_sem even
  though they would produce zero contracts — burning a semaphore slot and a
  Tradier API call per ticker per build cycle.
  Fix: read t3_min_oi from thresh (default 100); symbols below that floor
  are excluded from prebuild_tier_map entirely so they are never dispatched.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Module-level imports so patch('services.symbol_registry.*') targets work
# in unit tests (H3 fix - lazy imports inside methods are not patchable via
# the module namespace).
from services.ingestion_config import get_config
from services.tier_engine import _fetch_thresholds, assign_tiers
from services.chain_store import load_chain
from utils.tradier_client import get_expirations, get_option_chain_bulk, get_quotes_batch

log = logging.getLogger("symbol_registry")

# CONCURRENCY-10: lowered from 50 -> 10 to stay under Tradier's 120 req/min
# rate limit and prevent HTTP 400/429s that blocked full ~3,900 ticker coverage.
# Companion: tradier_client._BULK_CHAIN_SEM=10, max_connections=30.
_DEFAULT_BUILD_CONCURRENCY = 10

# BUILD-HANG: hard timeouts for the two network-bound phases inside build().
_PRICES_FETCH_TIMEOUT_S = 45    # _fetch_stock_prices(): 3949 tickers x 200/batch = 20 batches

# FIX-GATHER-TIMEOUT-18000: raised from 1800s -> 18000s.
_CHAIN_GATHER_TIMEOUT_S = 18000

# Per-request timeout for each individual get_option_chain_bulk() call (per-expiry).
_CHAIN_REQUEST_TIMEOUT_S: float = 45.0

# LOG-CHAIN-V2: stall warning threshold.
_CHAIN_STALL_WARN_S: float = 10.0

# LOG-CHAIN: log chain-pull progress every N tickers.
_CHAIN_PROGRESS_INTERVAL = 250

# FLUSH-PERIODIC: flush partial registry to DB every N seconds during gather.
_CHAIN_FLUSH_INTERVAL_S: int = 30

# FIX-DEAD-TICKER: number of consecutive empty get_expirations() returns
# before a ticker is added to _dead_ticker_set and permanently skipped.
_DEAD_TICKER_THRESHOLD: int = 3

# FIX-DEAD-TICKER: module-level state (persists across refresh_loop() rebuilds).
# Cleared only on process restart.
_dead_ticker_strikes: dict[str, int] = {}   # symbol -> consecutive empty-expiry count
_dead_ticker_set:     set[str]        = set()  # confirmed dead; skipped in _build_with_sem


@dataclass
class ContractMeta:
    ticker:        str
    strike:        float
    expiry:        str
    contract_type: str
    dte:           int
    open_interest: int
    tier:          int = 3


@dataclass
class _TierParams:
    atm_pct: float
    max_dte: int
    min_oi:  int


def _build_tier_params(thresh: dict, global_min_oi: int) -> dict[int, _TierParams]:
    # FIX-TIER-DEFAULTS: fallback values now match live DB tier_thresholds and
    # _DEFAULT_THRESHOLDS in tier_engine.py.
    # DB live values: T1(atm=0.15, dte=60, oi=1000) T2(atm=0.20, dte=90, oi=500)
    #                 T3(atm=0.20, dte=90, oi=100)
    # These fallbacks only fire when thresh.get() returns nothing (i.e. when
    # _fetch_thresholds() itself had to fall back to _DEFAULT_THRESHOLDS).
    return {
        1: _TierParams(
            atm_pct = float(thresh.get("t1_atm_pct", 0.15)),
            max_dte = int(thresh.get("t1_max_dte",   60)),
            min_oi  = max(global_min_oi, int(thresh.get("t1_min_oi", 1000))),
        ),
        2: _TierParams(
            atm_pct = float(thresh.get("t2_atm_pct", 0.20)),
            max_dte = int(thresh.get("t2_max_dte",   90)),
            min_oi  = max(global_min_oi, int(thresh.get("t2_min_oi", 500))),
        ),
        3: _TierParams(
            atm_pct = float(thresh.get("t3_atm_pct", 0.20)),
            max_dte = int(thresh.get("t3_max_dte",   90)),
            min_oi  = max(global_min_oi, int(thresh.get("t3_min_oi", 100))),
        ),
    }


class SymbolRegistry:
    """
    Layer-1 OCC contract registry.

    Attributes
    ----------
    epoch : int
        Monotonically-incrementing build generation counter.
        Starts at 0; incremented inside the build() lock immediately after
        ``_build_complete`` is set to True.
    """

    def __init__(
        self,
        watchlist: Optional[list[str]] = None,
        tier_map:  Optional[dict[str, int]] = None,
    ):
        self._watchlist: list[str]      = watchlist or []
        self._tier_map:  dict[str, int] = tier_map  or {}
        self._registry:  dict[str, ContractMeta] = {}
        self._stock_prices: dict[str, float]     = {}
        self._last_build: Optional[datetime]     = None
        self._build_lock = asyncio.Lock()
        self._oi_by_ticker: dict[str, int] = {}
        self._persisted_snapshot_id: Optional[str] = None
        self._volume_by_ticker: dict[str, int] = {}
        self._avg_volume_by_ticker: dict[str, int] = {}
        self._build_complete: bool = False
        self.epoch: int = 0

    def lookup(self, occ_symbol: str) -> Optional[ContractMeta]:
        return self._registry.get(occ_symbol.strip())

    def all_symbols(self) -> list[str]:
        return list(self._registry.keys())

    def size(self) -> int:
        return len(self._registry)

    def stock_price(self, ticker: str) -> float:
        return self._stock_prices.get(ticker, 0.0)

    def is_ready(self) -> bool:
        return self._build_complete

    def set_tier_map(self, tier_map: dict[str, int]) -> None:
        self._tier_map = tier_map

    def get_oi_map(self) -> dict[str, int]:
        return dict(self._oi_by_ticker)

    def influence_tier_int(self, ticker: str) -> int:
        return self._tier_map.get(ticker, 3)

    async def load_from_db(self, snapshot_id: Optional[str] = None) -> int:
        if not snapshot_id:
            log.info("[symbol_registry] load_from_db: no snapshot_id — skipping pre-seed")
            return 0
        chain = await load_chain(snapshot_id)
        if chain is None:
            log.info(
                "[symbol_registry] load_from_db: DB error for snapshot %s - "
                "skipping pre-seed, full build() will populate registry",
                snapshot_id,
            )
            return 0
        if not chain:
            log.info(
                "[symbol_registry] load_from_db: no cached chain for snapshot %s "
                "(including fallback) - will do full build from Tradier",
                snapshot_id,
            )
            return 0
        self._registry = chain
        self._persisted_snapshot_id = snapshot_id
        oi_acc: dict[str, list[int]] = {}
        for meta in chain.values():
            oi_acc.setdefault(meta.ticker, []).append(meta.open_interest)
        self._oi_by_ticker = {
            t: round(sum(v) / len(v)) for t, v in oi_acc.items() if v
        }
        log.info(
            "[symbol_registry] load_from_db: seeded %d OCC contracts from DB "
            "(snapshot %s, oi_map=%d tickers) - waiting for build() to set "
            "_build_complete before stream workers are allowed to spawn",
            len(chain), snapshot_id, len(self._oi_by_ticker),
        )
        return len(chain)

    async def build(self) -> tuple[int, dict[str, dict]]:
        """
        Build (or incrementally refresh) the OCC registry.
        See module docstring for full change history.
        """
        from services.symbols_loader import SymbolQuote

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        tier_params = _build_tier_params(thresh, global_min_oi=cfg["REGISTRY_MIN_OI"])

        build_concurrency = int(cfg.get("REGISTRY_BUILD_CONCURRENCY", _DEFAULT_BUILD_CONCURRENCY))
        sem = asyncio.Semaphore(build_concurrency)

        async with self._build_lock:
            self._build_complete = False

            if self._registry:
                min_dte_by_ticker: dict[str, int] = {}
                for meta in self._registry.values():
                    cur = min_dte_by_ticker.get(meta.ticker, 9999)
                    if meta.dte < cur:
                        min_dte_by_ticker[meta.ticker] = meta.dte

                tickers_to_refresh = [
                    t for t in self._watchlist
                    if min_dte_by_ticker.get(t, 0) == 0
                ]
                tickers_to_carry   = [
                    t for t in self._watchlist
                    if min_dte_by_ticker.get(t, 0) > 0
                ]

                log.info(
                    "[symbol_registry] H3 incremental build: %d tickers to refresh "
                    "(expired today), %d carried forward (total watchlist=%d)",
                    len(tickers_to_refresh), len(tickers_to_carry), len(self._watchlist),
                )
            else:
                tickers_to_refresh = list(self._watchlist)
                tickers_to_carry   = []
                log.info(
                    "[symbol_registry] Full build: %d tickers (concurrency=%d) "
                    "[T1: atm=+/-%.0f%% dte=%d | T2: atm=+/-%.0f%% dte=%d | "
                    "T3: atm=+/-%.0f%% dte=%d | min_oi=%d]",
                    len(tickers_to_refresh),
                    build_concurrency,
                    tier_params[1].atm_pct * 100, tier_params[1].max_dte,
                    tier_params[2].atm_pct * 100, tier_params[2].max_dte,
                    tier_params[3].atm_pct * 100, tier_params[3].max_dte,
                    cfg["REGISTRY_MIN_OI"],
                )

            # FIX-DEAD-TICKER: filter confirmed-dead tickers before building task list.
            # Log summary so operators can see how many quota slots are being saved.
            dead_skipped = [t for t in tickers_to_refresh if t in _dead_ticker_set]
            if dead_skipped:
                log.info(
                    "[symbol_registry] FIX-DEAD-TICKER: skipping %d confirmed-dead tickers "
                    "(saved ~%d Tradier API calls): %s%s",
                    len(dead_skipped),
                    len(dead_skipped),
                    ", ".join(dead_skipped[:20]),
                    "..." if len(dead_skipped) > 20 else "",
                )
            tickers_to_refresh = [t for t in tickers_to_refresh if t not in _dead_ticker_set]

            new_registry: dict[str, ContractMeta] = {
                occ: meta
                for occ, meta in self._registry.items()
                if meta.ticker in set(tickers_to_carry)
            }
            new_oi_by_ticker: dict[str, int] = {
                t: v
                for t, v in self._oi_by_ticker.items()
                if t in set(tickers_to_carry)
            }

            if tickers_to_carry:
                today_date = date.today()
                stale_occ = []
                for occ, meta in new_registry.items():
                    try:
                        exp_date = date.fromisoformat(meta.expiry)
                        fresh_dte = (exp_date - today_date).days
                        if fresh_dte < 0:
                            stale_occ.append(occ)
                        else:
                            meta.dte = fresh_dte
                    except (ValueError, AttributeError):
                        stale_occ.append(occ)
                for occ in stale_occ:
                    del new_registry[occ]
                if stale_occ:
                    log.info(
                        "[symbol_registry] FIX-STALE-DTE: evicted %d now-expired "
                        "carried contracts; %d remain",
                        len(stale_occ), len(new_registry),
                    )

            zero_price_fallback = False
            raw_quotes: dict[str, dict] = {}
            try:
                prices, raw_quotes = await asyncio.wait_for(
                    self._fetch_stock_prices(),
                    timeout=_PRICES_FETCH_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.error(
                    "[symbol_registry] BUILD-HANG: _fetch_stock_prices() timed out "
                    "after %ds — falling back to zero-price mode so chain fetches "
                    "still run (ATM filter bypassed).",
                    _PRICES_FETCH_TIMEOUT_S,
                )
                prices = {}
                zero_price_fallback = True

            if not prices:
                if not zero_price_fallback:
                    log.error(
                        "[symbol_registry] B-ZERO-PRICE: _fetch_stock_prices() returned "
                        "0 prices — ATM filter bypassed for all tickers."
                    )
                zero_price_fallback = True
            else:
                missing = [t for t in tickers_to_refresh if t not in prices or prices[t] <= 0]
                if missing:
                    log.warning(
                        "[symbol_registry] B-ZERO-PRICE partial: %d/%d tickers missing "
                        "price — those tickers will bypass ATM filter: %s",
                        len(missing), len(tickers_to_refresh),
                        ", ".join(missing[:20]) + ("..." if len(missing) > 20 else ""),
                    )

            self._stock_prices = prices

            # FIX-REFRESH-TIER-PREBUILD (2026-05-19): Refresh self._tier_map from
            # in-memory OI before dispatching _build_with_sem tasks.
            #
            # refresh_loop() calls build() directly, bypassing main.py's pre-fetch +
            # assign_tiers() sequence. Without this guard, every hourly chain pull
            # uses whatever _tier_map was stamped at startup, so symbols that shifted
            # tiers intraday get fetched with the wrong atm_pct / max_dte window.
            #
            # FIX-T3-MIN-OI-PRECLASSIFY (2026-05-19): Read t3_min_oi from thresh so
            # symbols below t3_min_oi=100 are excluded from prebuild_tier_map entirely.
            # Previously the else branch assigned tier=3 unconditionally, meaning
            # symbols with avg OI < 100 were still dispatched to _build_with_sem and
            # burned a semaphore slot + Tradier API call while producing zero contracts.
            _t1_min_oi = int(thresh.get("t1_min_oi", 1000))
            _t2_min_oi = int(thresh.get("t2_min_oi", 500))
            _t3_min_oi = int(thresh.get("t3_min_oi", 100))
            prebuild_tier_map: dict[str, int] = {}
            for ticker, avg_oi in self._oi_by_ticker.items():
                if avg_oi >= _t1_min_oi:
                    prebuild_tier_map[ticker] = 1
                elif avg_oi >= _t2_min_oi:
                    prebuild_tier_map[ticker] = 2
                elif avg_oi >= _t3_min_oi:
                    prebuild_tier_map[ticker] = 3
                # else: avg_oi < t3_min_oi — exclude entirely; no slot, no API call
            if prebuild_tier_map:
                self._tier_map.update(prebuild_tier_map)
                log.info(
                    "[symbol_registry] FIX-REFRESH-TIER-PREBUILD: pre-build tier_map "
                    "refreshed from in-memory OI (%d symbols — T1=%d T2=%d T3=%d; "
                    "t1_min_oi=%d t2_min_oi=%d t3_min_oi=%d)",
                    len(prebuild_tier_map),
                    sum(1 for t in prebuild_tier_map.values() if t == 1),
                    sum(1 for t in prebuild_tier_map.values() if t == 2),
                    sum(1 for t in prebuild_tier_map.values() if t == 3),
                    _t1_min_oi,
                    _t2_min_oi,
                    _t3_min_oi,
                )

            oi_acc_live: dict[str, list[int]] = {}
            counter = [0]
            gather_start = time.monotonic()

            snap_ref = [new_registry]

            async def _periodic_flush() -> None:
                from services.chain_store import save_chain
                while True:
                    await asyncio.sleep(_CHAIN_FLUSH_INTERVAL_S)
                    if not self._build_complete:
                        continue
                    snapshot = dict(snap_ref[0])
                    if not snapshot:
                        continue
                    if self._persisted_snapshot_id:
                        sid = self._persisted_snapshot_id
                    else:
                        sid = str(uuid.uuid4())
                    try:
                        await save_chain(sid, snapshot)
                        log.info(
                            "[symbol_registry] FLUSH-PERIODIC: flushed %d contracts "
                            "(snapshot_id=%s)",
                            len(snapshot), sid,
                        )
                    except Exception as exc:
                        log.warning(
                            "[symbol_registry] FLUSH-PERIODIC: flush failed — %s", exc
                        )

            flush_task = asyncio.ensure_future(_periodic_flush())

            async def _build_with_sem(ticker: str) -> None:
                try:
                    async with sem:
                        tier = self._tier_map.get(ticker, 3)
                        log.debug(
                            "[symbol_registry] BUILD START ticker=%s tier=%d",
                            ticker, tier,
                        )
                        t0 = time.monotonic()
                        result = await _build_ticker(
                            ticker=ticker,
                            stock_price=prices.get(ticker, 0.0),
                            tier_params=tier_params,
                            tier=tier,
                            zero_price_fallback=zero_price_fallback,
                        )
                        elapsed_ms = (time.monotonic() - t0) * 1000
                        log.debug(
                            "[symbol_registry] BUILD DONE  ticker=%s tier=%d "
                            "contracts=%d elapsed_ms=%.0f",
                            ticker, tier, len(result), elapsed_ms,
                        )

                        new_registry.update(result)
                        for meta in result.values():
                            oi_acc_live.setdefault(meta.ticker, []).append(meta.open_interest)

                        counter[0] += 1
                        n = counter[0]
                        total = len(tickers_to_refresh)
                        if n % _CHAIN_PROGRESS_INTERVAL == 0 or n == total:
                            elapsed_s = time.monotonic() - gather_start
                            rate = n / elapsed_s if elapsed_s > 0 else 0
                            eta_s = (total - n) / rate if rate > 0 else 0
                            log.info(
                                "[symbol_registry] chain progress: %d/%d (%.1f%%) "
                                "contracts=%d elapsed=%.0fs eta=%.0fs",
                                n, total, 100 * n / total,
                                len(new_registry),
                                elapsed_s, eta_s,
                            )
                except asyncio.CancelledError:
                    raise

            tasks = [_build_with_sem(t) for t in tickers_to_refresh]

            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_CHAIN_GATHER_TIMEOUT_S,
                )
                elapsed_gather = time.monotonic() - gather_start
                log.info(
                    "[symbol_registry] chain gather complete: %d tickers in %.0fs, "
                    "%d contracts accumulated",
                    len(tickers_to_refresh), elapsed_gather, len(new_registry),
                )
            except asyncio.TimeoutError:
                elapsed_gather = time.monotonic() - gather_start
                log.error(
                    "[symbol_registry] BUILD-HANG: chain gather timed out after %.0fs "
                    "(limit=%ds) — proceeding with partial registry (%d contracts)",
                    elapsed_gather, _CHAIN_GATHER_TIMEOUT_S, len(new_registry),
                )

            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass

            # Recompute OI averages.
            for ticker, oi_list in oi_acc_live.items():
                if oi_list:
                    new_oi_by_ticker[ticker] = round(sum(oi_list) / len(oi_list))

            # FIX C-3 REWRITE: Post-build OI-based tier reclassification.
            t1_min_oi = int(thresh.get("t1_min_oi", 1000))
            t2_min_oi = int(thresh.get("t2_min_oi", 500))
            reclassified: dict[str, int] = {}
            for ticker, avg_oi in new_oi_by_ticker.items():
                if avg_oi >= t1_min_oi:
                    reclassified[ticker] = 1
                elif avg_oi >= t2_min_oi:
                    reclassified[ticker] = 2
                else:
                    reclassified[ticker] = 3

            for occ, meta in new_registry.items():
                if meta.ticker in reclassified:
                    meta.tier = reclassified[meta.ticker]

            self._tier_map.update(reclassified)
            log.info(
                "[symbol_registry] post-build OI tier reclassification: "
                "%d tickers updated (t1_min_oi=%d, t2_min_oi=%d)",
                len(reclassified), t1_min_oi, t2_min_oi,
            )

            # FIX-DEAD-TICKER: log final dead-ticker summary for this build.
            log.info(
                "[symbol_registry] FIX-DEAD-TICKER build summary: "
                "dead_set_size=%d strikes_tracked=%d "
                "(dead tickers skipped entirely — quota recovered for live symbols)",
                len(_dead_ticker_set),
                len(_dead_ticker_strikes),
            )

            self._registry      = new_registry
            self._oi_by_ticker  = new_oi_by_ticker
            self._last_build    = datetime.utcnow()
            self._build_complete = True
            self.epoch          += 1

            log.info(
                "[symbol_registry] build() complete: epoch=%d contracts=%d "
                "tickers=%d",
                self.epoch, len(self._registry), len(self._oi_by_ticker),
            )

        await self._persist_to_db()

        return len(self._registry), raw_quotes

    async def _persist_to_db(self) -> None:
        from services.chain_store import save_chain
        if self._persisted_snapshot_id:
            snapshot_id = self._persisted_snapshot_id
        else:
            snapshot_id = str(uuid.uuid4())
            self._persisted_snapshot_id = snapshot_id

        ok = await save_chain(snapshot_id, self._registry)
        if not ok:
            log.warning(
                "[symbol_registry] _persist_to_db: save_chain returned False "
                "(snapshot_id=%s, contracts=%d) — DB write may have failed",
                snapshot_id, len(self._registry),
            )
        else:
            log.info(
                "[symbol_registry] _persist_to_db: saved %d contracts "
                "(snapshot_id=%s)",
                len(self._registry), snapshot_id,
            )

   