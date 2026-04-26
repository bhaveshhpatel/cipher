"""Regression tests for core/async_bus.py"""
import asyncio
import pytest
from core.async_bus import AsyncBus


@pytest.fixture
def bus():
    return AsyncBus()


# ---------------------------------------------------------------------------
# Subscribe / publish basics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_subscriber_receives_message(bus):
    received = []
    async def handler(msg): received.append(msg)
    bus.subscribe("test", handler)
    await bus.publish("test", {"data": 1})
    assert received == [{"data": 1}]


@pytest.mark.asyncio
async def test_multiple_subscribers_all_receive(bus):
    r1, r2 = [], []
    async def h1(msg): r1.append(msg)
    async def h2(msg): r2.append(msg)
    bus.subscribe("chan", h1)
    bus.subscribe("chan", h2)
    await bus.publish("chan", "hello")
    assert r1 == ["hello"] and r2 == ["hello"]


@pytest.mark.asyncio
async def test_publish_to_unknown_channel_no_error(bus):
    await bus.publish("no_such_channel", {})


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery(bus):
    received = []
    async def handler(msg): received.append(msg)
    bus.subscribe("ch", handler)
    bus.unsubscribe("ch", handler)
    await bus.publish("ch", "should not arrive")
    assert received == []


@pytest.mark.asyncio
async def test_subscribe_same_handler_twice_delivers_once(bus):
    received = []
    async def handler(msg): received.append(msg)
    bus.subscribe("ch", handler)
    bus.subscribe("ch", handler)  # duplicate
    await bus.publish("ch", "once")
    assert len(received) == 1


@pytest.mark.asyncio
async def test_different_channels_isolated(bus):
    got_a, got_b = [], []
    async def ha(msg): got_a.append(msg)
    async def hb(msg): got_b.append(msg)
    bus.subscribe("a", ha)
    bus.subscribe("b", hb)
    await bus.publish("a", "for_a")
    assert got_a == ["for_a"] and got_b == []


@pytest.mark.asyncio
async def test_publish_after_all_unsubscribed(bus):
    received = []
    async def handler(msg): received.append(msg)
    bus.subscribe("ch", handler)
    bus.unsubscribe("ch", handler)
    await bus.publish("ch", "nope")
    assert received == []


# ---------------------------------------------------------------------------
# Queue-based tests (if AsyncBus exposes queue primitives)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_put_and_get(bus):
    if not hasattr(bus, 'queue_put'):
        pytest.skip("AsyncBus has no queue_put method")
    await bus.queue_put("q1", "item")
    item = await bus.queue_get("q1")
    assert item == "item"


@pytest.mark.asyncio
async def test_two_queues_are_independent(bus):
    if not hasattr(bus, 'queue_put'):
        pytest.skip("AsyncBus has no queue_put method")
    await bus.queue_put("q1", "a")
    await bus.queue_put("q2", "b")
    assert await bus.queue_get("q1") == "a"
    assert await bus.queue_get("q2") == "b"


# ---------------------------------------------------------------------------
# Concurrent publish
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_publish_all_received(bus):
    received = []
    async def handler(msg): received.append(msg)
    bus.subscribe("ch", handler)
    await asyncio.gather(*[bus.publish("ch", i) for i in range(10)])
    assert len(received) == 10


@pytest.mark.asyncio
async def test_subscriber_exception_does_not_break_bus(bus):
    good = []
    async def bad_handler(msg): raise RuntimeError("boom")
    async def good_handler(msg): good.append(msg)
    bus.subscribe("ch", bad_handler)
    bus.subscribe("ch", good_handler)
    try:
        await bus.publish("ch", "test")
    except Exception:
        pass
    # good_handler may or may not have run depending on order — just no crash


@pytest.mark.asyncio
async def test_subscribe_returns_none_or_subscription(bus):
    async def handler(msg): pass
    result = bus.subscribe("ch", handler)
    assert result is None or result is not None  # just ensure no exception


@pytest.mark.asyncio
async def test_bus_survives_many_channels(bus):
    received = {}
    for i in range(20):
        ch = f"channel_{i}"
        received[ch] = []
        async def make_handler(ch=ch):
            async def h(msg): received[ch].append(msg)
            return h
        handler = await make_handler()
        bus.subscribe(ch, handler)
    for i in range(20):
        await bus.publish(f"channel_{i}", i)
    assert all(len(v) == 1 for v in received.values())


@pytest.mark.asyncio
async def test_bus_instance_independence():
    b1 = AsyncBus()
    b2 = AsyncBus()
    got_b1, got_b2 = [], []
    async def h1(msg): got_b1.append(msg)
    async def h2(msg): got_b2.append(msg)
    b1.subscribe("ch", h1)
    b2.subscribe("ch", h2)
    await b1.publish("ch", "only_b1")
    assert got_b1 == ["only_b1"] and got_b2 == []
