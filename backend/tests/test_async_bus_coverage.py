"""
Coverage boost for core/async_bus.py.
Targets: AsyncEventBus.unsubscribe (q-not-in-list), publish QueueFull,
         publish_all, AsyncBus.subscribe/unsubscribe/publish/publish_all.
"""
import asyncio

from core.async_bus import AsyncEventBus, AsyncBus


# --- AsyncEventBus ---

def test_event_bus_publish_delivers():
    async def _run():
        b = AsyncEventBus()
        q = b.subscribe("ch")
        await b.publish("ch", {"x": 1})
        return await q.get()
    result = asyncio.run(_run())
    assert result["x"] == 1


def test_event_bus_unsubscribe_unknown_queue_no_error():
    b = AsyncEventBus()
    q = asyncio.Queue()
    b.unsubscribe("missing_channel", q)  # should not raise
    b.subscribe("ch")
    b.unsubscribe("ch", q)               # q not in list — should not raise


def test_event_bus_publish_queue_full_drops_gracefully():
    async def _run():
        b = AsyncEventBus()
        q = b.subscribe("ch")
        # Fill the queue to maxsize
        for i in range(q.maxsize):
            await b.publish("ch", i)
        # This publish should silently drop (QueueFull)
        await b.publish("ch", "overflow")
        return q.qsize()
    size = asyncio.run(_run())
    assert size == 500  # maxsize, not 501


def test_event_bus_publish_all():
    async def _run():
        b = AsyncEventBus()
        q1 = b.subscribe("a")
        q2 = b.subscribe("b")
        await b.publish_all({"msg": "hi"})
        return await q1.get(), await q2.get()
    r1, r2 = asyncio.run(_run())
    assert r1["msg"] == "hi"
    assert r2["msg"] == "hi"


# --- AsyncBus (callback-based) ---

def test_async_bus_subscribe_and_publish():
    received = []
    async def handler(msg):
        received.append(msg)

    async def _run():
        b = AsyncBus()
        b.subscribe("ch", handler)
        await b.publish("ch", {"val": 42})

    asyncio.run(_run())
    assert received[0]["val"] == 42


def test_async_bus_subscribe_duplicate_ignored():
    async def handler(msg): pass

    b = AsyncBus()
    b.subscribe("ch", handler)
    b.subscribe("ch", handler)  # duplicate
    assert len(b._handlers["ch"]) == 1


def test_async_bus_unsubscribe():
    async def handler(msg): pass
    b = AsyncBus()
    b.subscribe("ch", handler)
    b.unsubscribe("ch", handler)
    assert len(b._handlers["ch"]) == 0


def test_async_bus_unsubscribe_missing_no_error():
    async def handler(msg): pass
    b = AsyncBus()
    b.unsubscribe("ch", handler)  # channel doesn't exist — no error


def test_async_bus_publish_all():
    received = {"a": [], "b": []}
    async def ha(msg): received["a"].append(msg)
    async def hb(msg): received["b"].append(msg)

    async def _run():
        b = AsyncBus()
        b.subscribe("a", ha)
        b.subscribe("b", hb)
        await b.publish_all({"x": 1})

    asyncio.run(_run())
    assert received["a"][0]["x"] == 1
    assert received["b"][0]["x"] == 1
