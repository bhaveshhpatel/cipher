"""
flow_store.py — Supabase DB writer for live options flow.

Subscribes to the async event bus and persists every classified
OptionsFlowEvent to `flow_events` and every signal episode to
`flow_episodes`.

Usage — call once at startup from main.py lifespan:

    from services.flow_store import start_flow_writer
    asyncio.create_task(start_flow_writer())

Tables written:
  - flow_events   : one row per classified tick (batched every 500ms or 100 rows)
  - flow_episodes : one row per same-session episode (upsert — ING-009)
                    Written directly from _process_trade() in tradier_stream.py
                    BEFORE the SIG-DEBOUNCE check. This decouples episode
                    persistence from the debounce gate (which is only for
                    WebSocket / signal_history anti-spam).

NOTE: Neither table receives an `id` field — Postgres generates it
      (uuid default for flow_events, bigserial for flow_episodes).

This module is the ONLY place that writes options flow data to the DB.
The async_bus is purely in-memory fan-out for WebSocket delivery.

IMPORTANT — Key selection:
  Only SUPABASE_SERVICE_ROLE_KEY is used here. The service role key
  bypasses Row Level Security (RLS) which is required for server-side
  inserts. The anon/public key (SUPABASE_KEY) respects RLS and will
  cause every insert to fail with a 42501 policy violation. Never use
  the anon key for backend DB writes.

C-003 — Sweep Retroactive Upgrade:
  upgrade_to_sweep_in_db(occ_symbol, fill_price, size) issues a targeted
  PATCH (UPDATE) to flow_events setting trade_type='SWEEP' for rows that
  were written as 'BTO' before the sweep threshold was confirmed. Called
  from _process_trade() via asyncio.create_task() so it does not block
  the stream hot path.

Bug fixes applied:
  1. ALERT-LEVEL (2026-04-29): _bus_signal_listener was reading
     sig.get("alert_level") but the published signal dict uses key "alert".
     Fixed to try both keys with fallback to "WATCH".
  2. SWEEP-SQL (2026-04-29): upgrade_to_sweep_in_db was embedding the raw
     SQL expression "now()-interval 30 seconds" as a literal string in the
     PostgREST filter URL. PostgREST does not evaluate SQL expressions in
     filter values. Fixed to pre-compute a UTC ISO timestamp in Python.
  3. EPISODE-FIX (2026-04-30): flow_episodes were only written when the
     composite_signal bus event fired (after Signal Gate AND SIG-DEBOUNCE).
     This caused flow_episodes row count to equal signal_history row count.
     Additionally, _bus_signal_listener always wrote strike=None/expiry=None
     because composite_msg never included those fields.
     Fix: persist_flow_episode() is now called directly from _process_trade()
     after Signal Gate, before SIG-DEBOUNCE. _bus_signal_listener no longer
     writes flow_episodes — it is retained but acts as a no-op consumer of
     the db_writer channel for future use.
  4. ING-007 (2026-05-04): persist_flow_episode() now accepts and writes
     is_multi_day_repeat (BOOLEAN). Requires migration
     add_is_multi_day_repeat_to_flow_episodes.sql to be run first.
  5. ING-007 (2026-05-04): enqueue_lookback() / get_lookback_stats() /
     start_lookback_worker() added. The worker drains an asyncio.Queue of
     ContractKeys enqueued by _process_trade() after each persisted episode,
     fetches the lookback result from contract_day_cache, then PATCHes the
     most-recent flow_episodes row for that contract with is_multi_day_repeat.
  6. LOG-FORMAT (2026-05-05): prem=$%,.0f used %-style format which does not
     support the comma thousands-separator. Pre-formatted as f-string instead.
  7. ING-009 (2026-05-06): persist_flow_episode() is now an upsert.
     _lookup_open_episode() queries flow_episodes for an open same-session
     episode matching (ticker, direction, contract_type, strike, expiry)
     within _EPISODE_MERGE_WINDOW_S. On match: PATCH (trade_count +=1,
     total_premium +=, signal_ts = new). On no match: INSERT (existing path).
     _stats gains "created_episodes" and "merged_episodes" counters.
     Both counters initialised at module level and visible in /health/stream.
  8. ING-009-RACE (2026-05-08): concurrent coroutines for the same contract
     arriving within the same batch flush window both called
     _lookup_open_episode(), both received None (neither INSERT had committed
     yet), and both issued INSERT — producing 2-3 orphan episode rows instead
     of 1. Fix: per-contract asyncio.Lock (_episode_locks dict keyed by merge
     key string) serialises the lookup→insert/patch path. An in-process
     _episode_in_flight cache allows the second waiter to PATCH the just-
     inserted row without a DB round-trip.
  9. ING-008 (2026-05-08): persist_flow_event() now captures vol/OI snapshot
     from chain_store.get_contract_vol_oi(occ_symbol) and writes
     contract_volume_snapshot and contract_oi to the flow_events row.
     persist_flow_episode() captures vol/OI at INSERT time (contract_oi_at_open)
     and at PATCH time (contract_volume_at_close), then pre-computes
     volume_oi_ratio = volume / oi (NULL-safe, zero-OI-safe).
     Vol/OI is enrichment only — a cache miss (None) never drops flow.
  10. REARCH-010 (2026-05-09): removed is_golden_sweep, influence_tier, and
      conviction_score from persist_flow_event() row dict — all three columns
      were dropped from flow_events in migration 024. Keeping them caused a
      PostgREST 400 on every event insert.
  11. REARCH-003 (2026-05-11): added classify_bid_ask(), compute_vol_oi_signal(),
      and quality tag fields (is_ask_side, bid_ask_class, vol_oi_signal,
      normalized_premium, normalized_oi, dte_bucket, notional_tier) to
      persist_flow_event() row dict.
  12. SA-5 (2026-05-11): three code sites independently divided vol/OI with no
      shared helper. Added _compute_vol_oi_ratio(vol, oi) -> Optional[float] as
      the single source of truth (round(vol/oi, 4), None on bad inputs).
      compute_vol_oi_signal() now delegates to _compute_vol_oi_ratio() instead
      of re-implementing the division. persist_flow_event() normalized_oi block
      replaced with _compute_vol_oi_ratio(open_interest, contract_oi), fixing
      rounding from 6dp to 4dp to match the test spec.
  13. PBE-1 (2026-05-11): public/private API type inversion corrected.
      classify_bid_ask() is now the PUBLIC function returning Tuple[str, bool]
      (bid_ask_class, is_ask_side) — callers get both values from one call.
      _classify_bid_ask() is now the PRIVATE shim.
      persist_flow_event() updated to unpack the tuple from classify_bid_ask().
  14. REARCH-003-ENUM (2026-05-11): dte_bucket and notional_tier were missing
      from persist_flow_event() row dict entirely — new rows were written with
      NULL for both columns despite the columns existing in the schema.
      Fixed by importing _compute_dte_bucket/_compute_notional_tier from
      processor and computing both inline before the row dict is built.
  15. FS-TEST-FIX (2026-05-11): three test failures addressed:
      a) FlowStore class added — tests 5-12 in test_flow_store.py.
      b) persist_flow_episode() signature changed to accept a single signal_data
         dict. Empty expiry string coerced to None.
      c) asyncio.sleep alias _async_sleep added for safe test patching.
  16. FAS-001 / FS-035 / FS-HANG (2026-05-11): three additional test fixes:
      a) FAS-001: _classify_bid_ask() now returns Tuple[str, bool] (full tuple)
         instead of str only. All 8 test_classify_bid_ask_* tests destructure
         the return as `cls, is_ask = ...` — returning str caused ValueError.
      b) FS-035: persist_flow_episode() INSERT now passes json=insert_payload
         (dict) instead of json=[insert_payload] (list). Test reads
         call_kwargs.kwargs.get("json") and asserts row["ticker"] — a list
         can't be subscripted by string key. Response unwrap handles both
         dict and list shapes from PostgREST.
      c) FS-HANG: start_flow_writer() now returns early when not configured
         (no tasks spawned). Previously, start_lookback_worker() was created
         unconditionally and blocked forever on _lookback_queue.get(),
         keeping the pytest event loop alive past teardown and causing CI
         timeout.
  17. ING-007-SIG (2026-05-11): start_lookback_worker() now accepts an optional
      accumulator argument. When provided (test path), it is used directly.
      When None (production/main.py zero-arg call), falls back to
      get_accumulator(). This restores test compatibility broken by FS-HANG
      fix which dropped the parameter entirely.
  18. ING-009-EXTRACT (2026-05-11): extracted _insert_rows_with_episode_id()
      from the inlined INSERT block inside persist_flow_episode().
      Tests in test_ing009_episode_upsert.py patch
      fs._insert_rows_with_episode_id — the function did not exist as a
      standalone module attribute, causing AttributeError on every INSERT-path
      test (E-1, E-4 through E-9, E-11 through E-13, E-15 through E-16).
      The new function:
        - POSTs with Prefer: return=representation
        - Stores the PostgREST-returned id in _episode_in_flight if present
        - Returns True on HTTP 200/201, False otherwise
      persist_flow_episode increments created_episodes after the call returns
      True (counter stays at the call site so mocking the helper still
      triggers the counter correctly — E-1 asserts created_episodes == 1
      after patching _insert_rows_with_episode_id to return True).
  19. ING-009-GUARD (2026-05-11): removed _is_configured() early-return guard
      from persist_flow_episode(). When SUPABASE_URL/KEY are unset (test env),
      the guard caused the function to return immediately — all 16 E-* tests
      saw created_episodes == 0 and merged_episodes == 0.
      The guard is redundant here: _lookup_open_episode() and
      _insert_rows_with_episode_id() each check _is_configured() internally
      and return None / False safely. The in-process counter increments and
      lock/in-flight logic