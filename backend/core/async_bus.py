"""
In-process async event bus. Signals are published here by the stream processor
and consumed by WebSocket connections.

Two implementations are exported:
  AsyncEventBus  — queue-based (original), used by routers/ws.py
  AsyncBus       — callback-based, used by tests and newer consumers
"""
import asyncio
from typing import Any, Callable, Dict, List


# ---------------------------------------------------------------------------
# AsyncEventBus  (queue-based, original)
# ---------------------------------------------------------------------------

class AsyncEventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.setdefault(channel, []).append(q)
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(channel, [])
        if q in subs:
            subs.remove(q)

    async def publish(self, channel: str, data: Any) -> None:
        for q in list(self._subscribers.get(channel, [])):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow consumer — drop

    async def publish_all(self, data: Any) -> None:
        for channel in list(self._subscribers.keys()):
            await self.publish(channel, data)


# ---------------------------------------------------------------------------
# AsyncBus  (callback-based)
# ---------------------------------------------------------------------------

class AsyncBus:
    """Callback-based async event bus.

    Usage::

        bus = AsyncBus()

        async def handler(msg): ...
        bus.subscribe("channel", handler)

        await bus.publish("channel", {"data": 1})
        bus.unsubscribe("channel", handler)
    """

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}

    def subscribe(self, channel: str, handler: Callable) -> None:
        handlers = self._handlers.setdefault(channel, [])
        if handler not in handlers:
            handlers.append(handler)

    def unsubscribe(self, channel: str, handler: Callable) -> None:
        handlers = self._handlers.get(channel, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, channel: str, data: Any) -> None:
        for handler in list(self._handlers.get(channel, [])):
            await handler(data)

    async def publish_all(self, data: Any) -> None:
        for channel in list(self._handlers.keys()):
            await self.publish(channel, data)


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

bus = AsyncEventBus()   # used by routers/ws.py
