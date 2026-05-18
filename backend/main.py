"""
Cipher Backend — FastAPI entry point

Startup sequence:
  0. gate_config_store.load()          — load tier gate config from DB into memory  [ING-010]
  1. validate_ingestion_config()       — warn on missing ingestion config rows  [RC-3]
  2. _resolve_startup_universe_fast()  — DB-only path: fresh snapshot HIT -> done;
                                         MISS -> seed with stale snapshot and schedule
                                         background refresh  [RENDER-STARTUP-HANG]
  3. init_registry()                   — in-memory init (instant)
  4. registry.load_from_db(snapshot_id) — seed OCC chains from DB via P1 fallback
  5. yield                             — SERVER IS LIVE, health probe passes (~6 s)
  6. Parallel background tasks launched:
     a. _background_build_and_upsert   — incremental/full OCC build (P4);
                                         sets _registry_build_done event on completion
     b. _background_universe_resolve() — MISS only: load_universe() + save + tier assign
                                         + stream_task relaunch with fresh symbols
                                         [STREAM-RELOAD]
     c. registry.refresh_loop()        — scheduled 30-min rebuilds
     d. _registry_prewarm_loop()       — 9:15 AM ET daily pre-warm
     e. stream_options_flow()          — waits for is_ready(), then streams
     f. start_flow_writer()            — DB flush loop
     g. start_signal_writer()          — signal DB writer
     h. _universe_refresh_loop()       — 24h universe refresh
     i. _chain_refresh_after_build()   — SEQ-002: awaits _registry_build_done Event,
                                         then sleeps 60 s (SEQ-002-STAGGER: quota
                                         recovery after build+upsert burst), THEN
                                         starts 5-min chain cache refresh loop
     j. _self_ping_worker()            — RENDER: pings /health every 5 min to prevent spin-down
     k. gate_config_store.start_refresh_loop(300) — ING-010-RELOAD: re-polls gate_configs
                                         from DB every 5 min so externally-written gate
                                         changes (Supabase dashboard, SQL, separate deploy)
                                         propagate to the running worker without a restart.
                                         Max stale-config window: 300 s.

Key architectural fixes:
  P1 (chain_store)    — snapshot-agnostic fallback load
  P2 (lifespan)       — non-blocking startup via background build task
  P3 (tradier_client) — dedicated bulk semaphore for build()
  P4 (symbol_registry)— incremental warm-restart build
  D-001 (tradier_stream) — pass registry to stream_options_flow(), no duplicate build
  D-002 (tradier_stream) — remove extra refresh_loop() create_task from stream
  RC-3 (ingestion_config) — validate_ingestion_config() at startup warns on missing DB rows
  H1   (main)         — _post_build_upsert reuses raw_quotes from build();
                        no duplicate _fetch_batch_quotes call on warm-restart
  M-1/M-2 (symbol_registry) — _build_complete flag; is_ready() no longer fires
                        on first DB-seeded contract; stream workers spawn only
                        after build() fully completes with fresh Tradier data
  M-3  (main)         — _post_build_upsert split into two guarded phases;
                        assign_tiers() failure raises so upsert_symbol_quotes()
                        is skipped and the error is visible in logs/metrics
  STREAM-5 (main)     — graceful shutdown: stream_task cancelled and awaited
                        FIRST so Tradier HTTP connections close cleanly before
                        the process exits, freeing session quota for the next
                        container start immediately.
  ING-010 (main)      — gate_config_store.load() is step 0 in startup so all
                        tier gate values are hot in memory before any service
                        (stream, accumulator, parser) runs its first tick.
  ING-010-ACC (main)  — after registry.set_tier_map(), tradier_stream.accumulator
                        .set_tier_map() is also called so the module-level hot-path
                        accumulator receives the same tier map. Without this, Gate 2
                        in _get_episode_min_premium() resolves every ticker to tier 1
                        (strict cold-start default) for the entire trading session.
  ING-010-RELOAD (main) — gate_config_store.start_refresh_loop(300) launched as
                        background task (Step 6-k). Re-polls gate_configs every
                        5 min so external DB writes propagate without a restart.
                        Root cause of May 15 leak: T1 min_premium was set to
                        $75,000 on May 7 via Supabase dashboard but the running
                        worker held the old $25,000 default from startup -- the
                        cache was frozen for the entire session lifetime.
  ING-008 (main)      — start_chain_refresh_worker() launched as a background task
                        after yield. Refreshes options chain vol/OI for all
                        stream_eligible symbols every 5 minutes via Tradier chain API.
                        Zero live API calls on the flow hot path -- persist_flow_event
                        and persist_flow_episode read from the in-process cache only.
                        FIX: wired with correct two-callable signature:
                          get_tracked_symbols -- lambda returning live OCC symbol list
                          fetch_chain_fn      -- _fetch_tradier_chain(symbol) helper
                        invalidate_vol_oi_cache() called at market-open boundary in
                        _registry_prewarm_loop() so yesterday's volume never bleeds.
  REARCH-002 (main)   — ingestion_config router mounted: GET/PATCH /admin/ingestion-config
                        now reachable. Previously the router was created but never
                        included in app.include_router().
  REARCH-005 (main)   — signal_config router mounted: GET/PATCH /admin/signal-config
                        now reachable. Reads/writes signal_config table; enforces
                        premium pyramid, DTE window, tier multiplier, and floor
                        ordering invariants. Calls reload_signal_config() on every
                        successful PATCH for immediate in-process snapshot refresh.
  MAIN-FIX-001        — start_lookback_worker() was refactored (FS-HANG) to fetch
                        its own accumulator internally via get_accumulator() -- it
                        takes 0 positional args. Removed stale registry.accumulator
                        argument from the create_task() call site.
  RENDER-KEEPALIVE    — _self_ping_worker() pings GET /health every 5 minutes.
                        Reads RENDER_EXTERNAL_URL env var (injected automatically by
                        Render). No-ops locally when the var is absent. Prevents
                        free-tier spin-down (15-min idle threshold).
                        Hardened (HOTFIX-KEEPALIVE-001):
                          - 5-min interval (down from 10) -- more headroom vs threshold
                          - Fresh httpx.AsyncClient each cycle -- avoids stale pool
                          - fail_streak counter -- log.warning per failure,
                            log.error after 3 consecutive failures
                        Does NOT prevent restarts caused by deploys, crashes, or OOM
                        -- tradier_stream reconnect logic handles those cases.
  RENDER-STARTUP-HANG — _resolve_startup_universe() split into a fast DB-only phase
                        (before yield) and a slow background phase (after yield).
                        The slow load_universe() call (CBOE + Tradier, 60-120 s) no
                        longer blocks yield, so Render's health probe passes in ~6 s
                        from cold start instead of timing out and restarting the
                        container in a loop.
  HOTFIX-IMPORT-001   — gate_config_store import corrected: the module exports `store`,
                        not `gate_config_store`. Fixed as `store as gate_config_store`.
                        This was crashing uvicorn on every boot with ImportError before
                        the lifespan even started.
  HOTFIX-CHAIN-HOURS  — _fetch_tradier_chain() now returns [] immediately outside
                        market hours (Mon-Fri 9:15 AM - 4:30 PM ET; never on weekends).
                        Tradier's chain endpoint returns HTTP 400 when markets are
                        closed, flooding logs with warnings all night. The guard is
                        co-located in the fetch helper so the chain_refresh_task loop
                        itself is untouched -- it simply sleeps its normal interval
                        between empty-return calls.
  HOTFIX-KEEPALIVE-001 — _self_ping_worker() hardened:
                        - interval dropped to 5 min (was 10) -- more margin vs 15-min
                          Render spin-down threshold
                        - httpx.AsyncClient recreated each cycle -- avoids stale
                          connection pool that causes silent failures on Render
                        - fail_streak counter replaces silent except-swallow;
                          log.warning on every failure, log.error after streak >= 3
                          with actionable guidance to check RENDER_EXTERNAL_URL
  SEQ-001 (main)      — _chain_refresh_after_build() wrapper introduced.
                        chain_refresh_worker was previously launched in parallel with
                        _background_build_and_upsert(), causing it to fire Tradier
                        chain API requests for all 3900 tickers while the OCC symbol
                        map was still being populated. This produced HTTP 400 storms
                        and incomplete chain data.
                        Fix: poll registry.is_ready() every 5 s (max 30 min) before
                        delegating to start_chain_refresh_worker(). The two fetch
                        paths are now strictly serialized:
                          symbol_registry.build() completes -> chain_refresh starts.
                        All other tasks (stream, db_write, signal_write, etc.) are
                        unaffected and continue to launch in parallel as before.
  SEQ-002 (main)      — Replace SEQ-001 polling with asyncio.Event (_registry_build_done).
                        Root cause of SEQ-001 failure: registry.is_ready() fires as soon
                        as _build_complete is set inside build(), which happens before
                        _post_build_upsert() completes. On warm restarts the registry is
                        pre-seeded from DB (7275 contracts) and _build_complete can be set
                        for a partial incremental build -- earlier than intended.
                        Fix: _registry_build_done = asyncio.Event() created in lifespan().
                          _background_build_and_upsert() sets it in a finally block after
                          both build() and _post_build_upsert() have finished (or failed).
                          _chain_refresh_after_build() awaits the event with a 30-min
                          timeout safety valve, then delegates to start_chain_refresh_worker().
                        The event fires exactly once, guaranteed, even on build failure or
                        CancelledError -- chain_refresh is never permanently blocked.
                        Zero polling, zero race conditions, zero timing ambiguity.
  SEQ-002-FIX (main)  — Correct the event-set placement in _background_build_and_upsert.
                        The previous implementation set build_done_event inside the
                        finally block of the try/except around registry.build() only --
                        meaning the event fired BEFORE _post_build_upsert() ran. This
   