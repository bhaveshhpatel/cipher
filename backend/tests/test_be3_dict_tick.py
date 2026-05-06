"""
tests/test_be3_dict_tick.py

BE-3 regression suite — _ev_attr / _make_key dict-event fix.

Acceptance criteria (Issue #47):
  AC-1  Two dict events for different tickers produce two distinct episode keys.
  AC-2  A dict event and an equivalent OptionsFlowEvent object for the same
        contract produce the identical episode key (key parity).
  AC-3  (non-functional) Raw dict events are a test-only path; the production
        stream worker always passes OptionsFlowEvent objects (confirmed below).

Post-merge findings addressed here:
  #49   Replace asyncio.get_event_loop().run_until_complete() with asyncio.run()
        (deprecated in 3.10+, RuntimeError in 3.12+).
  #50   Add contract_type isolation test: CALL vs PUT on same ticker/strike/expiry
        must produce two distinct episode keys.

Fix (2026-05-05):
  _make_acc previously passed dte_premium_tiers as a dict {9999: (1, 1)}.
  RepetitionAccumulator._get_episode_min_premium unpacks via:
    for max_dte, floors in self._dte_tiers:
  When _dte_tiers is a dict, iterating yields keys (ints), not (int, dict) tuples,
  causing TypeError: cannot unpack non-iterable int object.
  Fixed: pass list-of-tuples [(9999, {1:1, 2:1, 3:1})] and removed ignored min_premium kwarg.

Production-path note
--------------------
The Tradier stream worker constructs OptionsFlowEvent objects via
OptionsFlowParser.parse() before calling ingest_tick(); raw dict events never
reach RepetitionAccumulator in production.  Dict support is retained so that
unit and integration tests can construct events inline without importing the
parser, and to guard against regressions if the call site ever changes.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from signals.repetition_accumulator import RepetitionAccumulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_acc(**kwargs) -> RepetitionAccumulator:
    """Minimal accumulator — low thresholds so every test event qualifies.

    dte_premium_tiers must be a list of (max_dte, {tier: floor}) tuples.
    A single catch-all bucket covering all DTE with floor=1 ensures every
    event clears Gate 2 regardless of DTE or tier.
    """
    defaults = dict(
        min_trades=1,
        dte_premium_tiers=[(9999, {1: 1, 2: 1, 3: 1})],
        min_sweeps=0,
        aggression_discount=1.0,
    )
    defaults.update(kwargs)
    return RepetitionAccumulator(**defaults)


def _dict_event(
    ticker: str,
    contract_type: str = "CALL",
    strike: float = 150.0,
    expiry: str = "2026-06-20",
    premium: float = 100_000.0,
    trade_type: str = "SWEEP",
    dte: int = 15,
    underlying_price: float = 150.0,
    order_side: str = "BUY",
) -> dict:
    return {
        "ticker": ticker,
        "contract_type": contract_type,
        "strike": strike,
        "expiry": expiry,
        "premium": premium,
        "trade_type": trade_type,
        "dte": dte,
        "underlying_price": underlying_price,
        "order_side": order_side,
        "timestamp": datetime.now(timezone.utc),
        "is_aggressive": True,
    }


def _run(coro):
    # F2 fix: asyncio.run() is the correct forward-compatible pattern.
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AC-1 — distinct tickers → distinct episode keys
# ---------------------------------------------------------------------------

class TestDictTickDistinctKeys:
    """BE-3 AC-1: dict events for different contracts never collapse."""

    def test_two_tickers_two_keys(self):
        """AAPL CALL and TSLA PUT must land in separate episodes."""
        acc = _make_acc()
        ev_aapl = _dict_event("AAPL", contract_type="CALL", strike=150.0,
                              expiry="2026-06-20")
        ev_tsla = _dict_event("TSLA", contract_type="PUT",  strike=200.0,
                              expiry="2026-07-18")

        _run(acc.ingest_tick(ev_aapl))
        _run(acc.ingest_tick(ev_tsla))

        keys = list(acc._episodes.keys())
        assert len(keys) == 2, (
            f"Expected 2 distinct episode keys; got {len(keys)}: {keys}\n"
            "Regression: dict events are collapsing into one None|None|0.00|None key."
        )

    def test_same_ticker_different_strike(self):
        """Same ticker/type but different strike must produce two keys."""
        acc = _make_acc()
        ev_150 = _dict_event("AAPL", strike=150.0, expiry="2026-06-20")
        ev_160 = _dict_event("AAPL", strike=160.0, expiry="2026-06-20")

        _run(acc.ingest_tick(ev_150))
        _run(acc.ingest_tick(ev_160))

        keys = list(acc._episodes.keys())
        assert len(keys) == 2, (
            f"Strike isolation failed; got {len(keys)} keys: {keys}"
        )

    def test_same_ticker_different_expiry(self):
        """Same ticker/type/strike but different expiry must produce two keys."""
        acc = _make_acc()
        ev_jun = _dict_event("AAPL", expiry="2026-06-20")
        ev_jul = _dict_event("AAPL", expiry="2026-07-18")

        _run(acc.ingest_tick(ev_jun))
        _run(acc.ingest_tick(ev_jul))

        keys = list(acc._episodes.keys())
        assert len(keys) == 2, (
            f"Expiry isolation failed; got {len(keys)} keys: {keys}"
        )

    def test_same_ticker_different_contract_type(self):
        """CALL and PUT on the same ticker/strike/expiry must produce two distinct keys.

        F3 regression guard: if _make_key ever drops contract_type from the key,
        a CALL campaign and a PUT campaign on the same name would silently merge
        into one episode, corrupting direction accounting.
        """
        acc = _make_acc()
        ev_call = _dict_event("AAPL", contract_type="CALL", strike=150.0,
                              expiry="2026-06-20")
        ev_put  = _dict_event("AAPL", contract_type="PUT",  strike=150.0,
                              expiry="2026-06-20")

        _run(acc.ingest_tick(ev_call))
        _run(acc.ingest_tick(ev_put))

        keys = list(acc._episodes.keys())
        assert len(keys) == 2, (
            f"Contract-type isolation failed; got {len(keys)} keys: {keys}\n"
            "A CALL and PUT on the same ticker/strike/expiry must not share an episode."
        )

    def test_keys_never_none_components(self):
        """No key component should be the string 'None' after the fix.

        Episode key format: '{ticker}:{contract_type}:{strike}:{expiry}'
        Split on ':' and check no component is 'None'.
        """
        acc = _make_acc()
        ev = _dict_event("SPY", contract_type="CALL", strike=500.0,
                         expiry="2026-09-19")
        _run(acc.ingest_tick(ev))

        key = next(iter(acc._episodes.keys()))
        # Key format uses ':' separator
        parts = key.split(":")
        for part in parts:
            assert part != "None", (
                f"Key component is literal 'None': key={key!r}. "
                "_ev_attr is still using getattr() on a raw dict."
            )


# ---------------------------------------------------------------------------
# AC-2 — key parity: dict event and object event → same key
# ---------------------------------------------------------------------------

class TestDictObjectKeyParity:
    """BE-3 AC-2: a dict event and the equivalent OptionsFlowEvent object
    must hash to exactly the same episode key."""

    def _object_event(
        self,
        ticker="AAPL",
        contract_type="CALL",
        strike=150.0,
        expiry="2026-06-20",
        premium=100_000.0,
        trade_type="SWEEP",
        dte=15,
        underlying_price=150.0,
        order_side="BUY",
    ):
        """Return a minimal object whose attributes mirror the dict event."""
        class _Ev:
            pass
        ev = _Ev()
        ev.ticker           = ticker
        ev.contract_type    = contract_type
        ev.strike           = strike
        ev.expiry           = expiry
        ev.premium          = premium
        ev.trade_type       = trade_type
        ev.dte              = dte
        ev.underlying_price = underlying_price
        ev.order_side       = order_side
        ev.is_aggressive    = True
        ev.timestamp        = datetime.now(timezone.utc)
        return ev

    def _key_for(self, ev) -> str:
        """Ingest ev, return the resulting episode key."""
        acc = _make_acc()
        _run(acc.ingest_tick(ev))
        return next(iter(acc._episodes.keys()))

    def test_dict_and_object_produce_same_key(self):
        """dict event and object event for the same contract → identical key."""
        d_ev = _dict_event("AAPL", contract_type="CALL", strike=150.0,
                           expiry="2026-06-20")
        o_ev = self._object_event(ticker="AAPL", contract_type="CALL",
                                  strike=150.0, expiry="2026-06-20")

        key_dict = self._key_for(d_ev)
        key_obj  = self._key_for(o_ev)

        assert key_dict == key_obj, (
            f"Key parity failed:\n  dict key   = {key_dict!r}\n"
            f"  object key = {key_obj!r}\n"
            "_ev_attr diverges between dict and attribute-based access."
        )

    def test_dict_and_object_accumulate_to_same_episode(self):
        """Interleaved dict + object events for the same contract share an episode."""
        acc = _make_acc(min_trades=2)
        d_ev = _dict_event("NVDA", contract_type="CALL", strike=900.0,
                           expiry="2026-08-15")
        o_ev = self._object_event(ticker="NVDA", contract_type="CALL",
                                  strike=900.0, expiry="2026-08-15")

        _run(acc.ingest_tick(d_ev))
        _run(acc.ingest_tick(o_ev))

        assert len(acc._episodes) == 1, (
            f"Expected 1 shared episode; got {len(acc._episodes)} episodes. "
            "Dict and object events for the same contract must share a key."
        )
        ep = next(iter(acc._episodes.values()))
        assert ep.trade_count == 2, (
            f"Expected trade_count=2; got {ep.trade_count}. "
            "Second event was not appended to the existing episode."
        )


# ---------------------------------------------------------------------------
# Bonus: total_premium accuracy on multi-dict episode
# ---------------------------------------------------------------------------

class TestDictEpisodePremiumAccumulation:
    """dict events properly accumulate premium inside the episode."""

    def test_total_premium_two_dict_events_same_contract(self):
        acc = _make_acc()
        ev1 = _dict_event("AMZN", premium=200_000.0)
        ev2 = _dict_event("AMZN", premium=300_000.0)

        _run(acc.ingest_tick(ev1))
        _run(acc.ingest_tick(ev2))

        assert len(acc._episodes) == 1
        ep = next(iter(acc._episodes.values()))
        assert ep.total_premium == pytest.approx(500_000.0), (
            f"total_premium={ep.total_premium}; expected 500_000.0"
        )
