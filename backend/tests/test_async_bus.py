"""
Unit tests for core/async_bus.py

Covers:
  subscribe
  1.  Returns an asyncio.Queue
  2.  Queue has maxsize=500
  3.  Multiple subscribers on same channel each get their own queue
  4.  Subscribing to different channels is independent
  5.  Same subscriber can subscribe twice (two distinct queues)

  unsubscribe
  6.  Unsubscribed queue is removed from channel
  7.  Unsubscribing a queue not in channel is a no-op
  8.  Unsubscribing from unknown channel is a no-op
  9.  After unsubscribe, published messages do not reach removed queue
  10. Other queues on same channel still receive after one unsubscribes

  publish
  11. Published item is received by subscriber
  12. Published item is received by all subscribers on channel
  13. Full queue is silently dropped (no exception)
  14. Publishing to channel with no subscribers is a no-op
  15. Published item preserves type and value (dict, str, int)
  16. Multiple sequential publishes arrive in FIFO order

  publish_all
  17. Item delivered to all channels
  18. Channels with no subscribers are skipped safely
  19. Returns without error when no channels exist

  isolation
  20. Message published to channel A does not appear on channel B

  singleton
  21. `bus` import is the same object across imports
  22. `bus` is an AsyncEventBus instance

  edge cases
  23. Subscribe, publish, unsubscribe, re-subscribe cycle works correctly
  24. QueueFull on one subscriber does not prevent delivery to others
"""
import asyncio
from asyncio import Queue

from core.async_bus import AsyncEventBus, bus


# ── fixture ─────────────────────────────────────────────────────────────────────
def fresh():
    """Return a new, isolated AsyncEventBus for each test."""
    return AsyncEventBus()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# subscribe
# ============================================================

# 1
def test_subscribe_returns_queue():
    b = fresh()
    q = b.subscribe("ch")
    assert isinstance(q, Queue)


# 2
def test_subscribe_queue_maxsize_500():
    b = fresh()
    q = b.subscribe("ch")
    assert q.maxsize == 500


# 3
def test_subscribe_multiple_subscribers_get_distinct_queues():
    b = fresh()
    q1 = b.subscribe("ch")
    q2 = b.subscribe("ch")
    assert q1 is not q2


# 4
def test_subscribe_different_channels_independent():
    b = fresh()
    qa = b.subscribe("a")
    qb = b.subscribe("b")
    assert "a" in b._subscribers
    assert "b" in b._subscribers
    assert qa not in b._subscribers["b"]
    assert qb not in b._subscribers["a"]


# 5
def test_subscribe_same_subscriber_twice_gives_two_queues():
    b = fresh()
    _q1 = b.subscribe("ch")
    _q2 = b.subscribe("ch")
    assert len(b._subscribers["ch"]) == 2


# ============================================================
# unsubscribe
# ============================================================

# 6
def test_unsubscribe_removes_queue():
    b = fresh()
    q = b.subscribe("ch")
    b.unsubscribe("ch", q)
    assert q not in b._subscribers.get("ch", [])


# 7
def test_unsubscribe_queue_not_in_channel_noop():
    b = fresh()
    b.subscribe("ch")
    foreign_q = Queue()
    b.unsubscribe("ch", foreign_q)  # must not raise


# 8
def test_unsubscribe_unknown_channel_noop():
    b = fresh()
    b.unsubscribe("nonexistent", Queue())  # must not raise


# 9
def test_unsubscribe_stops_message_delivery():
    b = fresh()
    q = b.subscribe("ch")
    b.unsubscribe("ch", q)
    run(b.publish("ch", {"x": 1}))
    assert q.empty()


# 10
def test_unsubscribe_one_leaves_others_intact():
    b = fresh()
    q1 = b.subscribe("ch")
    q2 = b.subscribe("ch")
    b.unsubscribe("ch", q1)
    run(b.publish("ch", "hello"))
    assert q2.get_nowait() == "hello"
    assert q1.empty()


# ============================================================
# publish
# ============================================================

# 11
def test_publish_item_received_by_subscriber():
    b = fresh()
    q = b.subscribe("ch")
    run(b.publish("ch", {"ticker": "AAPL"}))
    assert q.get_nowait() == {"ticker": "AAPL"}


# 12
def test_publish_item_received_by_all_subscribers():
    b = fresh()
    q1 = b.subscribe("ch")
    q2 = b.subscribe("ch")
    q3 = b.subscribe("ch")
    run(b.publish("ch", 42))
    assert q1.get_nowait() == 42
    assert q2.get_nowait() == 42
    assert q3.get_nowait() == 42


# 13
def test_publish_full_queue_dropped_silently():
    b = fresh()
    q = b.subscribe("ch")  # maxsize=500
    # Fill queue to capacity
    for i in range(500):
        q.put_nowait(i)
    # This publish should be silently dropped, not raise
    run(b.publish("ch", "overflow"))
    assert q.full()


# 14
def test_publish_no_subscribers_noop():
    b = fresh()
    run(b.publish("ch", "data"))  # must not raise


# 15
def test_publish_preserves_type_and_value():
    b = fresh()
    for payload in [{"a": 1}, "string", 99, [1, 2, 3], None]:
        b2 = fresh()
        q = b2.subscribe("ch")
        run(b2.publish("ch", payload))
        assert q.get_nowait() == payload


# 16
def test_publish_fifo_order():
    b = fresh()
    q = b.subscribe("ch")
    for i in range(5):
        run(b.publish("ch", i))
    received = [q.get_nowait() for _ in range(5)]
    assert received == [0, 1, 2, 3, 4]


# ============================================================
# publish_all
# ============================================================

# 17
def test_publish_all_reaches_all_channels():
    b = fresh()
    qa = b.subscribe("a")
    qb = b.subscribe("b")
    qc = b.subscribe("c")
    run(b.publish_all("broadcast"))
    assert qa.get_nowait() == "broadcast"
    assert qb.get_nowait() == "broadcast"
    assert qc.get_nowait() == "broadcast"


# 18
def test_publish_all_skips_channels_with_no_subscribers():
    b = fresh()
    # Manually add an empty list for a channel
    b._subscribers["empty"] = []
    run(b.publish_all("x"))  # must not raise


# 19
def test_publish_all_no_channels_noop():
    b = fresh()
    run(b.publish_all("x"))  # must not raise


# ============================================================
# isolation
# ============================================================

# 20
def test_publish_channel_a_not_delivered_to_channel_b():
    b = fresh()
    qa = b.subscribe("a")
    qb = b.subscribe("b")
    run(b.publish("a", "only-for-a"))
    assert qa.get_nowait() == "only-for-a"
    assert qb.empty()


# ============================================================
# singleton
# ============================================================

# 21
def test_singleton_same_object_across_imports():
    from core.async_bus import bus as bus2
    assert bus is bus2


# 22
def test_singleton_is_async_event_bus_instance():
    assert isinstance(bus, AsyncEventBus)


# ============================================================
# edge cases
# ============================================================

# 23
def test_subscribe_publish_unsubscribe_resubscribe_cycle():
    b = fresh()
    q1 = b.subscribe("ch")
    run(b.publish("ch", "first"))
    assert q1.get_nowait() == "first"
    b.unsubscribe("ch", q1)

    q2 = b.subscribe("ch")
    run(b.publish("ch", "second"))
    assert q2.get_nowait() == "second"
    assert q1.empty()  # old subscriber gets nothing


# 24
def test_full_queue_does_not_block_other_subscribers():
    _b = fresh()
    q_full = _b.subscribe("ch")
    q_ok   = _b.subscribe("ch")
    # Fill q_full
    for i in range(500):
        q_full.put_nowait(i)
    # Publish — q_full is dropped, q_ok still receives
    run(_b.publish("ch", "important"))
    assert q_ok.get_nowait() == "important"
