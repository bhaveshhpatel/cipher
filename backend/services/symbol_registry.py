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
  assign_tiers() requires list[SymbolQuote] objects (average_volume, last_price
  etc.) which are not available at post-build time — only raw oi_by_ticker dict
  exists. Fix: inline OI re-tier directly against thresh (already fetched at
  the top of build()). Each ticker in new_oi_by_ticker is compared to
  t1_min_oi / t2_min_oi thresholds; the resolved tier is stamped onto every
  ContractMeta in new_registry and stored in _tier_map. This is equivalent
  to what assign_tiers(require_oi=True) was intended to do but could not
  because it received oi_map= (a dict) instead of quotes= (list[SymbolQuote]).

FIX H1 (2026-04-27): build() now returns a tuple[int, dict[str, dict]]
  (count, raw_quotes). Callers that only need the count ignore the second
  element; _post_build_upsert passes raw_quotes to
  _post_build_upsert so it can skip the duplicate _fetch_batch_quotes call.

FIX H3 (2026-04-27): Removed _seeded_from_db flag entirely. The incremental
  build guard is now `if self._registry:` - the populated registry itself is
  the correct signal for an incremental refresh. This means scheduled
  refresh_loop() calls also get incremental DTE-based pruning instead of
  always doing a full rebuild after the first build()`.
  Module-level imports of get_config, _fetch_thresholds, assign_tiers, and
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
  Caught by bare except -> logged as WARNING per batch -> all 20 batches fai