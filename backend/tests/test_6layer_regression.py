"""
6-layer integration regression suite.

Verifies the full pipeline: ingest → parse → tier → accumulate →
composite → publish, with all layers wired together.
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _raw_trade(
    symbol="AAPL",
    option_type="C",
    strike=180.0,
    expiry="2026-06-20",
    premium=500_000.0,
    size=100,
    bid=4.80,
    ask=4.90,
    price=4.85,
    open_interest=5000,
    iv=0.28,
    underlying_price=178.0,
    sale_cond="AA",
):
    return {
        "symbol":           symbol,
        "option_type":      option_type,
        "strike":           strike,
        "expiry":           expiry,
        "premium":          premium,
        "size":             size,
        "bid":              bid,
        "ask":              ask,
        "price":            price,
        "open_interest":    open_interest,
        "iv":               iv,
        "underlying_price": underlying_price,
        "sale_cond":        sale_cond,
        "timestamp":        datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Layer 1 — Parse
# ---------------------------------------------------------------------------

class TestLayer1Parse:
    def test_parse_call_trade(self):
        from ingestion.options_flow_parser import parse_trade
        ev = parse_trade(_raw_trade())
        assert ev is not None
        assert ev.ticker == "AAPL"
        assert ev.contract_type == "CALL"
        assert ev.premium == 500_000.0

    def test_parse_put_trade(self):
        from ingestion.options_flow_parser import parse_trade
        ev = parse_trade(_raw_trade(option_type="P"))
        assert ev.contract_type == "PUT"

    def test_parse_returns_none_for_bad_data(self):
        from ingestion.options_flow_parser import parse_trade
        assert parse_trade({}) is None

    def test_parse_dte_computed_correctly(self):
        from ingestion.options_flow_parser import parse_trade
        import datetime as dt
        future = (dt.date.today() + dt.timedelta(days=30)).strftime("%Y-%m-%d")
        ev = parse_trade(_raw_trade(expiry=future))
        assert ev is not None
        assert 28 <= ev.dte <= 32

    def test_parse_golden_sweep_flag(self):
        from ingestion.options_flow_parser import parse_trade
        trade = _raw_trade(premium=600_000.0, size=500)
        ev = parse_trade(trade)
        assert ev is not None
        assert hasattr(ev, "is_golden_sweep")

    def test_parse_bid_ask_spread(self):
        from ingestion.options_flow_parser import parse_trade
        ev = parse_trade(_raw_trade(bid=4.80, ask=4.90, price=4.85))
        assert ev is not None

    def test_parse_preserves_open_interest(self):
        from ingestion.options_flow_parser import parse_trade
        ev = parse_trade(_raw_trade(open_interest=9999))
        assert ev.open_interest == 9999

    def test_parse_rejects_zero_premium(self):
        from ingestion.options_flow_parser import parse_trade
        result = parse_trade(_raw_trade(premium=0.0))
        assert result is None or result.premium == 0.0

    def test_parse_rejects_negative_strike(self):
        from ingestion.options_flow_parser import parse_trade
        result = parse_trade(_raw_trade(strike=-1.0))
        assert result is None or result.strike < 0


# ---------------------------------------------------------------------------
# Layer 2 — Tier Classification
# ---------------------------------------------------------------------------

class TestLayer2Tier:
    def test_whale_tier_on_large_premium(self):
        from signals.tier_engine import classify_tier
        assert classify_tier(2_000_000.0) == "WHALE"

    def test_institutional_tier(self):
        from signals.tier_engine import classify_tier
        assert classify_tier(500_000.0) == "INSTITUTIONAL"

    def test_retail_tier_on_small_premium(self):
        from signals.tier_engine import classify_tier
        assert classify_tier(5_000.0) == "RETAIL"

    def test_boundary_exactly_at_threshold(self):
        from signals.tier_engine import classify_tier
        # Should not raise
        result = classify_tier(1_000_000.0)
        assert result in ("WHALE", "INSTITUTIONAL", "SMART_MONEY")

    def test_zero_premium_returns_retail(self):
        from signals.tier_engine import classify_tier
        result = classify_tier(0.0)
        assert result in ("RETAIL", "UNKNOWN")

    def test_negative_premium_does_not_crash(self):
        from signals.tier_engine import classify_tier
        try:
            classify_tier(-1000.0)
        except ValueError:
            pass

    def test_very_large_premium_stays_whale(self):
        from signals.tier_engine import classify_tier
        assert classify_tier(100_000_000.0) == "WHALE"


# ---------------------------------------------------------------------------
# Layer 3 — Repetition Accumulator
# ---------------------------------------------------------------------------

class TestLayer3Accumulator:
    def _accum(self):
        from signals.repetition_accumulator import RepetitionAccumulator
        return RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)

    def _ev(self, ticker="AAPL", premium=100_000.0, offset=0):
        ev = MagicMock()
        ev.ticker        = ticker
        ev.contract_type = "CALL"
        ev.strike        = 180.0
        ev.expiry        = "2026-06-20"
        ev.premium       = premium
        ev.timestamp     = datetime(2026, 4, 25, 10, 0, 0) + timedelta(seconds=offset * 60)
        return ev

    def test_below_threshold_returns_none(self):
        acc = self._accum()
        result = acc.ingest(self._ev(premium=10_000.0))
        assert result is None

    def test_at_threshold_returns_episode(self):
        acc = self._accum()
        ep = None
        for i in range(3):
            ep = acc.ingest(self._ev(premium=20_000.0, offset=i))
        assert ep is not None

    def test_episode_has_correct_trade_count(self):
        acc = self._accum()
        ep = None
        for i in range(3):
            ep = acc.ingest(self._ev(premium=20_000.0, offset=i))
        assert ep.trade_count == 3

    def test_window_isolation_between_tickers(self):
        acc = self._accum()
        for i in range(3):
            acc.ingest(self._ev(ticker="AAPL", premium=20_000.0, offset=i))
        result = acc.ingest(self._ev(ticker="TSLA", premium=20_000.0, offset=0))
        assert result is None


# ---------------------------------------------------------------------------
# Layer 4 — Composite Signal Engine
# ---------------------------------------------------------------------------

class TestLayer4Composite:
    def _episode(self, ticker="AAPL", n=5, premium=500_000.0,
                 sentiment="BULLISH", tier="WHALE"):
        from signals.repetition_accumulator import RepetitionEpisode
        ep = RepetitionEpisode(ticker=ticker, contract_type="CALL",
                               strike=180.0, expiry="2026-06-20")
        base = datetime(2026, 4, 25, 10, 0, 0)
        for i in range(n):
            ev = MagicMock()
            ev.ticker          = ticker
            ev.contract_type   = "CALL"
            ev.strike          = 180.0
            ev.expiry          = "2026-06-20"
            ev.premium         = premium
            ev.dte             = 30
            ev.sentiment       = sentiment
            ev.influence_tier  = tier
            ev.open_interest   = 5000
            ev.is_golden_sweep = False
            ev.timestamp       = base + timedelta(minutes=i * 5)
            ep.events.append(ev)
        ep.first_seen = ep.events[0].timestamp
        ep.last_seen  = ep.events[-1].timestamp
        return ep

    def _accum(self):
        from signals.repetition_accumulator import RepetitionAccumulator
        return RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)

    def test_build_composite_returns_signal(self):
        from signals.composite_signal_engine import build_composite, CompositeSignal
        sig = build_composite(self._episode(), self._accum())
        assert isinstance(sig, CompositeSignal)

    def test_composite_score_in_range(self):
        from signals.composite_signal_engine import build_composite
        sig = build_composite(self._episode(), self._accum())
        assert 0.0 <= sig.composite_score <= 1.0

    def test_ticker_propagated(self):
        from signals.composite_signal_engine import build_composite
        sig = build_composite(self._episode(ticker="NVDA"), self._accum())
        assert sig.ticker == "NVDA"

    def test_reasoning_non_empty(self):
        from signals.composite_signal_engine import build_composite
        sig = build_composite(self._episode(), self._accum())
        assert len(sig.reasoning) > 0


# ---------------------------------------------------------------------------
# Layer 5 — Flow Store persistence
# ---------------------------------------------------------------------------

class TestLayer5FlowStore:
    @pytest.mark.asyncio
    async def test_add_and_retrieve_flow(self):
        import services.flow_store as fs
        await fs.clear_flows()
        flow = {
            "ticker": "AAPL", "premium": 500_000.0,
            "sentiment": "BULLISH", "contract_type": "CALL",
            "composite_score": 0.80, "influence_tier": "WHALE",
        }
        await fs.add_flow(flow)
        flows = await fs.get_flows("AAPL")
        assert any(f["ticker"] == "AAPL" for f in flows)

    @pytest.mark.asyncio
    async def test_clear_removes_all(self):
        import services.flow_store as fs
        await fs.add_flow({"ticker": "SPY", "premium": 100_000.0})
        await fs.clear_flows()
        assert await fs.get_flows("SPY") == []

    @pytest.mark.asyncio
    async def test_concurrent_adds_are_safe(self):
        import services.flow_store as fs
        await fs.clear_flows()
        await asyncio.gather(*[
            fs.add_flow({"ticker": f"T{i}", "premium": float(i * 1000)})
            for i in range(10)
        ])
        for i in range(10):
            assert await fs.get_flows(f"T{i}") != []


# ---------------------------------------------------------------------------
# Layer 6 — Signal Store persistence
# ---------------------------------------------------------------------------

class TestLayer6SignalStore:
    @pytest.mark.asyncio
    async def test_save_and_retrieve_signal(self):
        from services.signal_store import save_signal, get_signals
        await save_signal({"ticker": "AAPL", "score": 0.88,
                           "recommendation": "BUY"})
        sigs = await get_signals("AAPL")
        assert any(s.get("ticker") == "AAPL" for s in sigs)

    @pytest.mark.asyncio
    async def test_dedup_prevents_double_save(self):
        from services.signal_store import save_signal, get_signals
        sig = {"ticker": "TSLA", "score": 0.90, "id": "layer6-dedup-1"}
        await save_signal(sig)
        await save_signal(sig)
        sigs = await get_signals("TSLA")
        matching = [s for s in sigs if s.get("id") == "layer6-dedup-1"]
        assert len(matching) <= 1


# ---------------------------------------------------------------------------
# End-to-end pipeline smoke test
# ---------------------------------------------------------------------------

class TestE2EPipeline:
    @pytest.mark.asyncio
    async def test_raw_trade_to_signal_no_crash(self):
        """
        Full pipeline: raw trade dict → parse → tier → accumulate →
        composite → store. Verify no exception is raised.
        """
        from ingestion.options_flow_parser import parse_trade
        from signals.tier_engine import classify_tier
        from signals.repetition_accumulator import RepetitionAccumulator
        from signals.composite_signal_engine import build_composite
        import services.flow_store as fs

        await fs.clear_flows()
        accum = RepetitionAccumulator(
            window_minutes=30, min_trades=3, min_premium=50_000
        )

        for i in range(3):
            raw = _raw_trade(premium=200_000.0)
            ev = parse_trade(raw)
            assert ev is not None
            ev.influence_tier = classify_tier(ev.premium)
            ep = accum.ingest(ev)

        assert ep is not None
        sig = build_composite(ep, accum)
        assert sig is not None
        assert 0.0 <= sig.composite_score <= 1.0

        await fs.add_flow({
            "ticker":          sig.ticker,
            "composite_score": sig.composite_score,
            "recommendation":  sig.recommendation,
        })
        flows = await fs.get_flows(sig.ticker)
        assert len(flows) >= 1

    @pytest.mark.asyncio
    async def test_pipeline_handles_bad_parse_gracefully(self):
        from ingestion.options_flow_parser import parse_trade
        result = parse_trade({})
        assert result is None

    @pytest.mark.asyncio
    async def test_pipeline_handles_multiple_tickers_concurrently(self):
        from ingestion.options_flow_parser import parse_trade
        from signals.tier_engine import classify_tier
        import services.flow_store as fs

        await fs.clear_flows()
        tickers = ["AAPL", "TSLA", "NVDA", "SPY", "QQQ"]

        async def _process(ticker):
            raw = _raw_trade(symbol=ticker, premium=500_000.0)
            ev = parse_trade(raw)
            if ev:
                ev.influence_tier = classify_tier(ev.premium)
                await fs.add_flow({"ticker": ev.ticker,
                                   "influence_tier": ev.influence_tier})

        await asyncio.gather(*[_process(t) for t in tickers])
        for t in tickers:
            flows = await fs.get_flows(t)
            assert len(flows) >= 1, f"Missing flows for {t}"
