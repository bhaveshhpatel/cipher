"""
services/gate_config_store.py — in-memory gate configuration store  [ING-010]

Provides a thin, thread-safe in-memory cache over the gate_configs table.
All hot-path gate reads (accumulator, parser, signal engine) go through
  store.get(gate_name, tier) -> float
which returns in O(1) without a DB round-trip.

Updates are committed to the DB first, then the in-memory cache is
patched atomically via store.update() so there is never a window where
the DB has a new value but memory still has the old one.

Return contract:
  store.get(gate_name, t