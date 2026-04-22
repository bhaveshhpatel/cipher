"""
In-process async event bus. Signals are published here by the stream processor
and consumed by WebSocket connections.
"""
import asyncio
from typing import Dict, List, Any

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

# Singleton
bus = AsyncEventBus()
