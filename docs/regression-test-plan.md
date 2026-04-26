# Cipher — Regression & Test Plan

> Last updated: 2026-04-25 (B-021 staggered startup · B-022 session-token semaphore · B-023 429 handler)

---

## Automated Test Coverage

### Auth
| Test | File | Status |
|------|------|--------|
| POST /auth/register — success 201 | `tests/test_auth.py` | ✅ |
| POST /auth/login — success 200 + JWT | `tests/test_auth.py` | ✅ |
| GET /auth/me — authenticated | `tests/test_auth.py` | ✅ |
| GET /auth/me — unauthenticated 401 | `tests/test_auth.py` | ✅ |

### OCC Parser — options_flow_parser.py (T-001)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| OP-1 | Standard CALL symbol parsed correctly | `test_parse_occ_call` | `tests/test_occ_parser.py` |
| OP-2 | Standard PUT symbol parsed correctly | `test_parse_occ_put` | `tests/test_occ_parser.py` |
| OP-3 | Long ticker (SPXW) parsed correctly | `test_parse_occ_long_ticker` | `tests/test_occ_parser.py` |
| OP-4 | Whitespace padding handled | `test_parse_occ_whitespace_padding` | `tests/test_occ_parser.py` |
| OP-5 | Invalid symbol returns None tuple | `test_parse_occ_invalid_symbol` | `tests/test_occ_parser.py` |
| OP-6 | Invalid date (month 13) returns None tuple | `test_parse_occ_invalid_date` | `tests/test_occ_parser.py` |
| OP-7 | Empty string returns None tuple | `test_parse_occ_empty_string` | `tests/test_occ_parser.py` |
| OP-8 | Strike correctly divided by 1000 | `test_parse_occ_strike_divided_by_1000` | `tests/test_occ_parser.py` |
| OP-9 | Future expiry returns positive DTE | `test_calc_dte_future` | `tests/test_occ_parser.py` |
| OP-10 | Empty expiry returns 0 DTE | `test_calc_dte_empty_string` | `tests/test_occ_parser.py` |
| OP-11 | Past expiry clamped to 0 | `test_calc_dte_past_clamped_to_zero` | `tests/test_occ_parser.py` |
| OP-12 | Unparseable expiry returns 0 | `test_calc_dte_unparseable` | `tests/test_occ_parser.py` |
| OP-13 | Epoch ms timestamp parsed correctly | `test_parse_timestamp_epoch_ms` | `tests/test_occ_parser.py` |
| OP-14 | ISO string timestamp parsed correctly | `test_parse_timestamp_iso_string` | `tests/test_occ_parser.py` |
| OP-15 | None timestamp falls back to utcnow | `test_parse_timestamp_none_returns_datetime` | `tests/test_occ_parser.py` |
| OP-16 | Garbage timestamp falls back to utcnow | `test_parse_timestamp_garbage_returns_datetime` | `tests/test_occ_parser.py` |
| OP-17 | CALL trade with all fields returns valid event | `test_parse_call_returns_event` | `tests/test_occ_parser.py` |
| OP-18 | PUT trade returns BEARISH sentiment | `test_parse_put_bearish_sentiment` | `tests/test_occ_parser.py` |
| OP-19 | CALL trade returns BULLISH sentiment | `test_parse_call_bullish_sentiment` | `tests/test_occ_parser.py` |
| OP-20 | `last` field used as primary fill (C-015) | `test_parse_last_field_primary_fill` | `tests/test_occ_parser.py` |
| OP-21 | `price` field used as fallback fill | `test_parse_price_field_fallback_fill` | `tests/test_occ_parser.py` |
| OP-22 | bid+ask mid used when last and price absent | `test_parse_mid_fill_when_no_last_or_price` | `tests/test_occ_parser.py` |
| OP-23 | Ticker from OCC prefix when `underlying` absent (C-010) | `test_parse_ticker_from_occ_when_no_underlying` | `tests/test_occ_parser.py` |
| OP-24 | Strike from OCC when stream field is 0 (C-011) | `test_parse_strike_from_occ_when_stream_zero` | `tests/test_occ_parser.py` |
| OP-25 | Expiry from OCC when stream field absent | `test_parse_expiry_from_occ_when_stream_absent` | `tests/test_occ_parser.py` |
| OP-26 | contract_type from OCC when option_type absent | `test_parse_contract_type_from_occ_when_option_type_absent` | `tests/test_occ_parser.py` |
| OP-27 | DTE auto-calculated when dte field is 0 (C-011) | `test_parse_dte_auto_calculated` | `tests/test_occ_parser.py` |
| OP-28 | `is_synthetic_quote=True` when bid=ask=0, fill>0 (C-018) | `test_parse_is_synthetic_quote_true_when_bid_ask_zero` | `tests/test_occ_parser.py` |
| OP-29 | `is_synthetic_quote=False` with real bid/ask | `test_parse_is_synthetic_quote_false_when_real_bid_ask` | `tests/test_occ_parser.py` |
| OP-30 | premium = fill × size × 100 | `test_parse_premium_formula` | `tests/test_occ_parser.py` |
| OP-31 | size=0 returns None | `test_parse_size_zero_returns_none` | `tests/test_occ_parser.py` |
| OP-32 | Malformed payload returns None (no exception) | `test_parse_malformed_payload_returns_none` | `tests/test_occ_parser.py` |
| OP-33 | influence_tier WHALE for premium ≥ 2M | `test_parse_influence_tier_whale` | `tests/test_occ_parser.py` |
| OP-34 | influence_tier INSTITUTIONAL for 500k–2M | `test_parse_influence_tier_institutional` | `tests/test_occ_parser.py` |
| OP-35 | influence_tier LARGE for 100k–500k | `test_parse_influence_tier_large` | `tests/test_occ_parser.py` |
| OP-36 | influence_tier RETAIL below 100k | `test_parse_influence_tier_retail` | `tests/test_occ_parser.py` |
| OP-37 | conviction_score in [0, 1] | `test_parse_conviction_score_in_range` | `tests/test_occ_parser.py` |
| OP-38 | is_golden_sweep is bool | `test_parse_is_golden_sweep_true` | `tests/test_occ_parser.py` |
| OP-39 | Registry enrichment overrides ticker/strike | `test_parse_registry_enrichment_overrides_fields` | `tests/test_occ_parser.py` |
| OP-40 | Registry failure is non-fatal | `test_parse_registry_failure_non_fatal` | `tests/test_occ_parser.py` |

### Bid/Ask Classifier + Trade Type Detector (T-001)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| CL-1 | fill > ask → ABOVE_ASK | `test_classify_above_ask` | `tests/test_classifier.py` |
| CL-2 | fill == ask → AT_ASK | `test_classify_at_ask` | `tests/test_classifier.py` |
| CL-3 | fill == bid → AT_BID | `test_classify_at_bid` | `tests/test_classifier.py` |
| CL-4 | fill < bid → BELOW_BID | `test_classify_below_bid` | `tests/test_classifier.py` |
| CL-5 | fill between bid/ask → MID | `test_classify_mid` | `tests/test_classifier.py` |
| CL-6 | Crossed market (bid > ask) → MID fallback | `test_classify_crossed_market_mid_fallback` | `tests/test_classifier.py` |
| CL-7 | All zeros → MID fallback | `test_classify_all_zeros_mid_fallback` | `tests/test_classifier.py` |
| CL-8 | Exact midpoint → MID | `test_classify_exact_midpoint` | `tests/test_classifier.py` |
| CL-9 | ABOVE_ASK → is_aggressive True | `test_is_aggressive_above_ask` | `tests/test_classifier.py` |
| CL-10 | AT_ASK → is_aggressive True | `test_is_aggressive_at_ask` | `tests/test_classifier.py` |
| CL-11 | MID → is_aggressive False | `test_is_aggressive_mid_false` | `tests/test_classifier.py` |
| CL-12 | AT_BID → is_aggressive False | `test_is_aggressive_at_bid_false` | `tests/test_classifier.py` |
| CL-13 | BELOW_BID → is_aggressive False | `test_is_aggressive_below_bid_false` | `tests/test_classifier.py` |
| CL-14 | Unknown class → is_aggressive False | `test_is_aggressive_unknown_false` | `tests/test_classifier.py` |
| CL-15 | exchange_count ≥ 3 → SWEEP | `test_detect_sweep_exchange_count` | `tests/test_classifier.py` |
| CL-16 | fill_count ≥ 3 → SPLIT | `test_detect_split_fill_count` | `tests/test_classifier.py` |
| CL-17 | premium ≥ 500k + size ≥ 50 → BLOCK | `test_detect_block` | `tests/test_classifier.py` |
| CL-18 | Fallback → SINGLE | `test_detect_single_fallback` | `tests/test_classifier.py` |
| CL-19 | SWEEP beats BLOCK when exchange_count ≥ 3 | `test_detect_sweep_over_block` | `tests/test_classifier.py` |
| CL-20 | SPLIT ≠ SWEEP (single exchange) | `test_detect_split_not_sweep_single_exchange` | `tests/test_classifier.py` |
| CL-21 | SWEEP + ≥1M + aggressive → golden sweep True | `test_is_golden_sweep_true` | `tests/test_classifier.py` |
| CL-22 | SWEEP + <1M → golden sweep False | `test_is_golden_sweep_false_low_premium` | `tests/test_classifier.py` |
| CL-23 | BLOCK + ≥1M + aggressive → golden sweep False | `test_is_golden_sweep_false_wrong_type` | `tests/test_classifier.py` |
| CL-24 | SWEEP + ≥1M + not aggressive → golden sweep False | `test_is_golden_sweep_false_not_aggressive` | `tests/test_classifier.py` |

### Repetition Accumulator (T-001)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| RA-1 | trade_count equals event list length | `test_episode_trade_count` | `tests/test_repetition_engine.py` |
| RA-2 | total_premium sums all events | `test_episode_total_premium` | `tests/test_repetition_engine.py` |
| RA-3 | is_accelerating True — last 3 within 60s | `test_episode_is_accelerating_true` | `tests/test_repetition_engine.py` |
| RA-4 | is_accelerating False — span > 60s | `test_episode_is_accelerating_false_long_span` | `tests/test_repetition_engine.py` |
| RA-5 | is_accelerating False — fewer than 3 events | `test_episode_is_accelerating_false_too_few_events` | `tests/test_repetition_engine.py` |
| RA-6 | summary_str contains contract_type, strike, expiry | `test_episode_summary_str_contains_key_fields` | `tests/test_repetition_engine.py` |
| RA-7 | ingest returns None below min_trades | `test_ingest_returns_none_below_min_trades` | `tests/test_repetition_engine.py` |
| RA-8 | ingest returns None below min_premium | `test_ingest_returns_none_below_min_premium` | `tests/test_repetition_engine.py` |
| RA-9 | ingest returns episode when both thresholds met | `test_ingest_returns_episode_when_thresholds_met` | `tests/test_repetition_engine.py` |
| RA-10 | Rolling window prunes stale events | `test_ingest_prunes_stale_events` | `tests/test_repetition_engine.py` |
| RA-11 | Different contracts keyed independently | `test_ingest_different_contracts_independent` | `tests/test_repetition_engine.py` |
| RA-12 | Same contract accumulates across calls | `test_ingest_accumulates_across_calls` | `tests/test_repetition_engine.py` |
| RA-13 | Episode returned on every qualifying call | `test_ingest_returns_episode_on_every_qualifying_call` | `tests/test_repetition_engine.py` |
| RA-14 | premium ≥ 5M → CONVICTION | `test_alert_level_conviction_high_premium` | `tests/test_repetition_engine.py` |
| RA-15 | accelerating + premium ≥ 1M → CONVICTION | `test_alert_level_conviction_accelerating` | `tests/test_repetition_engine.py` |
| RA-16 | premium ≥ 1M (not accelerating) → STRONG_SIGNAL | `test_alert_level_strong_signal` | `tests/test_repetition_engine.py` |
| RA-17 | premium ≥ 250k → ALERT | `test_alert_level_alert` | `tests/test_repetition_engine.py` |
| RA-18 | premium < 250k → WATCH | `test_alert_level_watch` | `tests/test_repetition_engine.py` |
| RA-19 | Default window is 30 minutes | `test_default_window_30_minutes` | `tests/test_repetition_engine.py` |
| RA-20 | Default min_trades is 3 | `test_default_min_trades_3` | `tests/test_repetition_engine.py` |
| RA-21 | Default min_premium is 50k | `test_default_min_premium_50k` | `tests/test_repetition_engine.py` |
| RA-22 | Custom params respected | `test_custom_params_respected` | `tests/test_repetition_engine.py` |

### Tradier Stream — Failure Mode Regression (F1–F9)
| Test ID | Failure Mode | Test Name | File |
|---------|-------------|-----------|------|
| F1 | Token re-fetched on every reconnect | `test_f1_token_fetched_per_reconnect` | `tests/test_tradier_stream.py` |
| F2 | 401 on stream does NOT permanently fall to demo | `test_f2_stream_401_does_not_permanently_fall_to_demo` | `tests/test_tradier_stream.py` |
| F3 | Idle watchdog triggers on dead connection | `test_f7_watchdog_raises_on_idle` | `tests/test_tradier_stream.py` |
| F4 | Backoff increases with attempt, cap respected | `test_increases_with_attempt`, `test_cap_respected` | `tests/test_tradier_stream.py` |
| F4 | Backoff has jitter (non-deterministic) | `test_jitter_non_deterministic` | `tests/test_tradier_stream.py` |
| F4 | Backoff base case (attempt=0) | `test_base_case_zero_attempt` | `tests/test_tradier_stream.py` |
| F5 | Session fetch retried 3x on timeout | `test_f5_retries_on_timeout` | `tests/test_tradier_stream.py` |
| F5 | Session fetch recovers on second attempt | `test_f5_succeeds_on_second_attempt` | `tests/test_tradier_stream.py` |
| F6a | Session 401 returns None (bad API key) | `test_returns_none_on_401` | `tests/test_tradier_stream.py` |
| F6a | Session missing sessionid returns None | `test_returns_none_on_missing_sessionid` | `tests/test_tradier_stream.py` |
| F7 | Watchdog passes when lines arrive in time | `test_f7_watchdog_passes_on_active_stream` | `tests/test_tradier_stream.py` |
| F8 | Demo mode task cancels cleanly | `test_f8_demo_mode_cancels_cleanly` | `tests/test_tradier_stream.py` |
| F8 | Demo mode emits valid signals | `test_f8_demo_mode_emits_signals` | `tests/test_tradier_stream.py` |
| F9 | Stats dict has all required keys + mode field | `test_get_stats_returns_dict`, `test_mode_field_exists` | `tests/test_tradier_stream.py` |

### Tradier Stream — Market-Hours Guard & Backoff Fix (commit 9a32d4b)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| MH-1 | `_is_market_hours()` returns False on Saturday | `test_market_hours_saturday_is_false` | `tests/test_tradier_stream.py` |
| MH-2 | `_is_market_hours()` returns False on Sunday | `test_market_hours_sunday_is_false` | `tests/test_tradier_stream.py` |
| MH-3 | `_is_market_hours()` returns False before 09:30 ET weekday | `test_market_hours_before_open_is_false` | `tests/test_tradier_stream.py` |
| MH-4 | `_is_market_hours()` returns False after 16:00 ET weekday | `test_market_hours_after_close_is_false` | `tests/test_tradier_stream.py` |
| MH-5 | `_is_market_hours()` returns True at 10:00 ET Monday | `test_market_hours_open_weekday_is_true` | `tests/test_tradier_stream.py` |
| MH-6 | Loop sleeps 60s and logs when market closed | `test_loop_sleeps_when_market_closed` | `tests/test_tradier_stream.py` |
| MH-7 | Mode set to `market_closed` when outside hours | `test_mode_is_market_closed_outside_hours` | `tests/test_tradier_stream.py` |
| BF-1 | `reconnect_attempt` resets to 0 when `session_ticks > 0` | `test_backoff_resets_when_ticks_received` | `tests/test_tradier_stream.py` |
| BF-2 | `reconnect_attempt` increments when `session_ticks == 0` | `test_backoff_increments_when_no_ticks` | `tests/test_tradier_stream.py` |
| BF-3 | Backoff reaches ~60s cap after 4+ zero-tick connections | `test_backoff_reaches_cap_after_zero_tick_closes` | `tests/test_tradier_stream.py` |

### Stream Worker — Staggered Startup (B-021)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| SW-B021-1 | First worker (index 0) starts with 0s delay | `test_first_worker_no_delay` | `tests/test_stream_worker_b021.py` |
| SW-B021-2 | Worker index 1 waits 200ms before token fetch | `test_second_worker_200ms_delay` | `tests/test_stream_worker_b021.py` |
| SW-B021-3 | Worker index 5 waits 1000ms before token fetch | `test_worker_index_5_delay_1000ms` | `tests/test_stream_worker_b021.py` |
| SW-B021-4 | `startup_delay_s` env override respected (e.g. 0.5s) | `test_startup_delay_env_override` | `tests/test_stream_worker_b021.py` |
| SW-B021-5 | Delay is `asyncio.sleep`, not blocking `time.sleep` | `test_startup_delay_is_async_sleep` | `tests/test_stream_worker_b021.py` |
| SW-B021-6 | 32-worker batch: last worker delay ≤ max_startup_window | `test_32_workers_max_delay_within_window` | `tests/test_stream_worker_b021.py` |
| SW-B021-7 | Stagger does not apply on reconnect (only first start) | `test_stagger_skipped_on_reconnect` | `tests/test_stream_worker_b021.py` |

### Tradier Client — Session Token Semaphore & 429 Handler (B-022 / B-023)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| TC-01 | Semaphore limits concurrent token fetches to 3 | `test_semaphore_limits_concurrency_to_3` | `tests/test_tradier_client.py` |
| TC-02 | 4th concurrent caller blocks until a slot is free | `test_4th_caller_blocks_until_slot_free` | `tests/test_tradier_client.py` |
| TC-03 | Semaphore released on successful fetch | `test_semaphore_released_on_success` | `tests/test_tradier_client.py` |
| TC-04 | Semaphore released on exception (no deadlock) | `test_semaphore_released_on_exception` | `tests/test_tradier_client.py` |
| TC-05 | HTTP 429 — `Retry-After` header respected; sleeps exact value | `test_429_retry_after_header_respected` | `tests/test_tradier_client.py` |
| TC-06 | HTTP 429 — missing `Retry-After` falls back to 60s default | `test_429_missing_retry_after_defaults_60s` | `tests/test_tradier_client.py` |
| TC-07 | HTTP 429 — retried up to max_retries then returns None | `test_429_exhausted_retries_returns_none` | `tests/test_tradier_client.py` |
| TC-08 | Non-429 HTTP error (e.g. 500) is not retried via 429 path | `test_non_429_not_retried` | `tests/test_tradier_client.py` |

### Stream Worker — Global Stats Rollup (B-008)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| SW-01 | `_inc_global_error()` increments `_stats["errors"]` | `test_inc_global_error_increments` | `tests/test_stream_worker_b008.py` |
| SW-02 | `_inc_global_reconnect()` increments `_stats["reconnects"]` | `test_inc_global_reconnect_increments` | `tests/test_stream_worker_b008.py` |
| SW-03 | `_inc_global_reconnect()` sets `last_reconnect_at` to float within wall-clock bounds | `test_inc_global_reconnect_sets_timestamp` | `tests/test_stream_worker_b008.py` |
| SW-04 | `_inc_global_error()` is safe when key absent (no crash) | `test_inc_global_error_safe_on_missing_key` | `tests/test_stream_worker_b008.py` |
| SW-05 | 5 concurrent workers all accumulate into same stats dict | `test_concurrent_workers_accumulate_stats` | `tests/test_stream_worker_b008.py` |

### Options Universe — symbols_loader.py
| Test ID | Scenario | File |
|---------|----------|------|
| SL-1 | Happy path — returns validated symbols list | `tests/test_symbols_loader.py` |
| SL-2 | Tradier 401 — returns seed fallback | `tests/test_symbols_loader.py` |
| SL-3 | Network error — returns seed fallback | `tests/test_symbols_loader.py` |
| SL-4 | Empty results from Tradier — returns seed fallback | `tests/test_symbols_loader.py` |
| SL-5 | Single-dict Tradier response (not list) — handled correctly | `tests/test_symbols_loader.py` |
| SL-6 | Lowercase symbols normalized to uppercase | `tests/test_symbols_loader.py` |
| SL-7 | Exception on single symbol does not abort whole batch | `tests/test_symbols_loader.py` |
| SL-8–20 | All 6 `load_universe()` fallback scenario branches | `tests/test_symbols_loader.py` |

### Options Universe — universe_store.py
| Test ID | Scenario | File |
|---------|----------|------|
| US-1 | `load_fresh_snapshot` — snapshot < 24h old returned | `tests/test_universe_store.py` |
| US-2 | `load_fresh_snapshot` — no rows → returns None | `tests/test_universe_store.py` |
| US-3 | `load_any_snapshot` — stale snapshot returned as fallback | `tests/test_universe_store.py` |
| US-4 | `save_snapshot` — empty symbol list rejected | `tests/test_universe_store.py` |
| US-5 | `save_snapshot` — insert failure handled | `tests/test_universe_store.py` |
| US-6 | DB exception propagates correctly | `tests/test_universe_store.py` |
| US-7 | Prunes to last 7 snapshots on save | `tests/test_universe_store.py` |
| US-8 | Batch insert fires for > 500 symbols | `tests/test_universe_store.py` |
| US-9 | `snapshot_id` generated via `uuid4()` in Python — passed in payload | `tests/test_universe_store.py` |
| US-10 | No `.select()` chained after `.insert()` (supabase-py v2 guard) | `tests/test_universe_store.py` |

### Flow Store — DB Signal Persistence (commit 701aaf6)
| Test ID | Scenario | Test Name | File |
|---------|----------|-----------|------|
| FS-1 | `flow_episode` row matches `flow_episodes` schema; no old `composite_signals` columns | `test_flow_episode_row_schema` | `tests/test_flow_store.py` |
| FS-2 | `flow_events` row has no `id` field (Postgres generates uuid) | `test_flow_event_row_no_id` | `tests/test_flow_store.py` |
| FS-3 | Sparse/None input produces safe defaults for all nullable fields | `test_flow_event_sparse_defaults` | `tests/test_flow_store.py` |
| FS-4 | f-string log with None/zero values does not raise | `test_fstring_log_none_fields_no_crash` | `tests/test_flow_store.py` |
| FS-5 | Buffer accumulates rows and drains atomically | `test_buffer_accumulate_and_drain` | `tests/test_flow_store.py` |
| FS-6 | `start_flow_writer` no-ops when SUPABASE_URL/KEY not set | `test_no_op_without_supabase_env` | `tests/test_flow_store.py` |
| FS-7 | `persist_flow_episode` calls `_insert_rows("flow_episodes", ...)` not `composite_signals` | `test_persist_flow_episode_calls_correct_table` | `tests/test_flow_store.py` |
| FS-8 | `persist_flow_event` buffers without any network call | `test_persist_flow_event_buffers_without_network` | `tests/test_flow_store.py` |

### Resolved Regressions — flow_store.py
| ID | Bug | Symptom | Root Cause | Fix (commit 701aaf6) |
|----|-----|---------|------------|----------------------|
| REG-FS-1 | Wrong DB table | 400 on every signal persist | `flow_store.py` posting to `composite_signals` (wrong schema) | Retargeted to `flow_episodes`; renamed function `persist_flow_episode` |
| REG-FS-2 | Client-sent `id` field | 400 on `flow_events` and `flow_episodes` inserts | `id` included in row dict; Postgres uuid/bigserial column rejects client-provided value | Removed `id` from both `_make_event_row` and `_make_episode_row` |
| REG-FS-3 | Log crash on None fields | `TypeError` in logging thread when signal fields are None | `log.info("... %,.0f", None)` — `%`-style defers eval to formatter, crashes on None | Switched all log calls to f-strings (evaluated immediately; None renders as "None") |

### Signal Pipeline
| Test | File | Status |
|------|------|--------|
| Flow scan response validation | `tests/test_flow.py` | ✅ |
| Stream stats response validation | `tests/test_stream.py` | ✅ |
| Simulation validation guardrails | `tests/test_simulation.py` | ✅ |
| WebSocket real-time signal receipt | `tests/test_ws.py` | ✅ |

---

## Phase 3 — Smart Signals, WS Heartbeat, Parser Guard, Volume-Weighted Score

### Parser — Size Field Guard (options_flow_parser.py)
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-P-1 | `raw` dict missing `size` key entirely | Returns `None` (no event emitted) |
| P3-P-2 | `raw["size"]` is `None` | Returns `None` |
| P3-P-3 | `raw["size"]` is `0` | Returns `None` |
| P3-P-4 | `raw["size"]` is `"0"` (string zero) | Returns `None` |
| P3-P-5 | `raw["size"]` is valid positive int | Returns valid `OptionsFlowEvent` |
| P3-P-6 | `raw["size"]` is valid positive string int | Returns valid `OptionsFlowEvent` |

### Signal Engine — Volume-Weighted Premium Factor
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-S-1 | `open_interest == 0` on latest event | `volume_weighted_premium_factor()` returns `0.5` |
| P3-S-2 | `open_interest` unavailable (field absent) | Returns `0.5` neutral |
| P3-S-3 | `total_premium / (OI × 100)` < 1.0 | Returns ratio rounded to 3dp |
| P3-S-4 | Very high premium vs low OI — ratio > 1.0 | Capped at `1.0` |
| P3-S-5 | Composite score uses weights `0.55/0.35/0.10` | `comp == flow*0.55 + bt*0.35 + vwp*0.10` |
| P3-S-6 | `CompositeSignal` includes `volume_premium_factor` field | Field present and 0–1 range |
| P3-S-7 | Reasoning string includes volume-premium factor | String contains `volume-premium factor` |

### Smart Signals Router — Pagination & Filters
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-R-1 | `GET /api/signals/list` — no params | 200, returns 20 results, page=1 |
| P3-R-2 | `GET /api/signals/list?page=2&page_size=5` | 200, returns up to 5 results, page=2 |
| P3-R-3 | `page_size=101` | 422 Unprocessable Entity |
| P3-R-4 | `page=0` | 422 Unprocessable Entity |
| P3-R-5 | `direction=bullish` | Returns only signals with `recommendation=="BUY"` |
| P3-R-6 | `direction=bearish` | Returns only signals with `recommendation=="SELL"` |
| P3-R-7 | `direction=neutral` | Returns only signals with `recommendation=="HOLD"` |
| P3-R-8 | `direction=invalid` | 422 with valid values listed |
| P3-R-9 | `tier=invalid` | 422 with valid values listed |
| P3-R-10 | `min_conviction=0.65` | Returns only signals where `composite_score >= 0.65` |
| P3-R-11 | `min_conviction=1.1` | 422 (exceeds max 1.0) |
| P3-R-12 | `min_conviction=-0.1` | 422 (below min 0.0) |
| P3-R-13 | Unauthenticated request | 401 |
| P3-R-14 | `CompositeOut` response includes `volume_premium_factor` | Field present |
| P3-R-15 | `total` in response reflects filtered count, not full count | Correct filtered total |

### WebSocket Heartbeat (ws.py)
| Test ID | Scenario | Expected |
|---------|----------|----------|
| P3-W-1 | Client connects, server sends `{"type":"ping"}` within 25s | Ping received |
| P3-W-2 | Client responds `{"type":"pong"}` within 10s | Connection stays open |
| P3-W-3 | Client does NOT respond to ping within 10s | Server closes with code 1001 |
| P3-W-4 | Client sends non-pong message in ping window | Server logs warning, connection may continue |
| P3-W-5 | `stop_event` set externally | Heartbeat task exits cleanly |
| P3-W-6 | WebSocket disconnect during heartbeat | `stop_event` set, task cancelled, bus unsubscribed |
| P3-W-7 | Invalid JWT on connect | Closed with code 4001, no heartbeat started |

### Manual Regression — Phase 3
- [ ] `GET /api/signals/list` returns paginated JSON with `signals`, `page`, `page_size`, `total`
- [ ] `?direction=bullish&min_conviction=0.65` filters correctly — all returned signals are BUY ≥ 0.65
- [ ] `?page=2&page_size=5` returns correct slice of results
- [ ] `GET /api/signals/composite/AAPL` response includes `volume_premium_factor` field
- [ ] Composite scores use new `0.55/0.35/0.10` weights (verify via reasoning string)
- [ ] Browser WS devtools: ping frame arrives every ~25s
- [ ] Browser WS devtools: pong reply sent by frontend keeps connection alive
- [ ] Railway logs: `WS pong timeout — closing connection` does NOT appear during normal operation
- [ ] Parser: flow events with missing/null/zero `size` do not reach accumulator (verify via Railway logs — no `prem=$0` entries)

---

## Running Tests

```bash
# All tests
cd backend
pip install -r requirements-dev.txt
pytest tests/ -v

# OCC parser, classifier, repetition engine (T-001)
pytest tests/test_occ_parser.py tests/test_classifier.py tests/test_repetition_engine.py -v

# Flow store tests only (DB persistence regression)
pytest tests/test_flow_store.py -v

# Tradier stream failure modes only
pytest tests/test_tradier_stream.py -v

# Market-hours guard tests only
pytest tests/test_tradier_stream.py -k "market_hours or backoff" -v

# B-021 staggered startup tests only
pytest tests/test_stream_worker_b021.py -v

# B-022 / B-023 semaphore + 429 handler tests only
pytest tests/test_tradier_client.py -v

# B-008 global stats rollup tests only
pytest tests/test_stream_worker_b008.py -v

# Universe tests only
pytest tests/test_symbols_loader.py tests/test_universe_store.py -v

# Live Tradier integration test (requires real key, skip in CI)
TRADIER_API_KEY=<your_key> pytest tests/test_tradier_stream.py \
    -k test_live_tradier_session_token -v
```

Equivalent manual curl for session token:
```bash
curl -X POST https://api.tradier.com/v1/markets/events/session \
     -H 'Authorization: Bearer <your_key>' \
     -H 'Accept: application/json' \
     -d ''
```

---

## Manual Regression Checklist

### Auth
- [ ] Register new account → 201, JWT returned
- [ ] Login → 200, JWT stored in localStorage
- [ ] Dashboard redirect when unauthenticated → `/login`
- [ ] `/me` with valid JWT → user object returned

### Dashboard
- [ ] Flow tab fetches and renders signal cards
- [ ] Swarm tab runs simulation and renders verdict
- [ ] Signal feed updates live via WebSocket
- [ ] Stream stats bar updates (ticks, signals, mode)
- [ ] Composite card loads correctly
- [ ] Frontend uses `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL`

### Tradier Stream (Railway logs)
- [ ] On deploy: logs show `Tradier session token obtained successfully`
- [ ] Logs show `Tradier stream connected — monitoring N symbols`
- [ ] After 5 min: logs show reconnect, NOT `401 — Falling back to demo mode`
- [ ] Mode in `/health` or stats endpoint shows `live` not `demo`
- [ ] **Outside market hours:** logs show `Market closed (ET: ...) — sleeping 60s` once per minute
- [ ] **Outside market hours:** `/health` mode shows `market_closed`
- [ ] **At market open (09:30 ET):** stream transitions from `market_closed` → `live` automatically
- [ ] **B-021:** Railway logs show workers starting at staggered intervals (~200ms apart) on cold boot
- [ ] **B-021:** No burst of simultaneous token-fetch requests at startup (verify via Tradier API logs)
- [ ] **B-022:** Under high worker-spawn load, never more than 3 concurrent `/markets/events/session` requests in flight
- [ ] **B-023:** If Tradier returns 429, Railway logs show `[tradier] 429 — sleeping Xs (Retry-After)` before retry
- [ ] **B-023:** After `Retry-After` sleep, token fetch resumes automatically — stream comes up without manual intervention

### DB Signal Persistence (flow_store.py) — commit 701aaf6
- [ ] Railway logs show `[flow_store] DB writer subscribed to bus — flow_episodes will be persisted`
- [ ] Railway logs show `[flow_store] flow_episode saved: <ticker> <contract_type> alert=<level> prem=$<amount>` (no crash, no None format error)
- [ ] Supabase `flow_episodes` table receives rows within seconds of signals qualifying
- [ ] Supabase `flow_events` table receives batched rows every ~5 seconds during market hours
- [ ] No rows appear in `composite_signals` table (old, wrong target — should be empty/absent)
- [ ] `flow_episodes.id` is auto-generated by Postgres (bigserial), not client-sent
- [ ] `flow_events.id` is auto-generated by Postgres (uuid), not client-sent
- [ ] Railway logs show `[flow_store] flushed N flow_events to DB` on each flush cycle

---

## Performance Scenarios (Backlog)
- Load test `/api/flow/scan` with 50 concurrent authenticated users
- WebSocket fan-out with 50+ concurrent subscribers
- Stream processor benchmark under 1k ticks/minute
- Frontend render profiling for 200-signal feed cap
- `flow_events` batch write latency under high tick volume (1k+/min)

## Test Count Summary

| Test File | IDs | Count |
|-----------|-----|-------|
| `test_occ_parser.py` | OP-1 – OP-40 | 40 |
| `test_classifier.py` | CL-1 – CL-24 | 24 |
| `test_repetition_engine.py` | RA-1 – RA-22 | 22 |
| `test_tradier_stream.py` | F1–F9, MH-1–7, BF-1–3 | ~27 |
| `test_tradier_client.py` | TC-01 – TC-08 | 8 |
| `test_stream_worker_b021.py` | SW-B021-1 – SW-B021-7 | 7 |
| `test_stream_worker_b008.py` | SW-01 – SW-05 | 5 |
| `test_symbols_loader.py` | SL-1–20 | ~20 |
| `test_universe_store.py` | US-1–10 | 10 |
| `test_flow_store.py` | FS-1–8 | 8 |
| `test_auth.py` | — | 4 |
| `test_flow.py`, `test_stream.py`, `test_simulation.py`, `test_ws.py` | — | ~4 |
| **Total** | | **~279** |
