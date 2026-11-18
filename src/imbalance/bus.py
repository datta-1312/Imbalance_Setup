"""Pub/sub between the engine and the API/websocket clients.

``InMemoryBus`` for the default single-process deployment; ``RedisBus`` when the
engine and the API run as separate processes (spec §8 suggested stack)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class InMemoryBus:
    def __init__(self, maxsize: int = 1000):
        self._subs: list[asyncio.Queue] = []
        self.maxsize = maxsize
        self.published = 0
        self.dropped = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def publish(self, topic: str, payload: Any) -> None:
        self.published += 1
        msg = {"topic": topic, "data": payload}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:   # a slow browser never slows the engine
                self.dropped += 1

    @property
    def subscribers(self) -> int:
        return len(self._subs)


class RedisBus(InMemoryBus):
    """Publishes to Redis channels; ``listen()`` relays a channel into local subscribers."""

    def __init__(self, url: str, prefix: str = "imbalance", maxsize: int = 1000):
        super().__init__(maxsize)
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(url)
        self.prefix = prefix
        self._pending: set[asyncio.Task] = set()

    def publish(self, topic: str, payload: Any) -> None:
        super().publish(topic, payload)
        data = json.dumps({"topic": topic, "data": payload}, default=str)
        task = asyncio.ensure_future(self.redis.publish(f"{self.prefix}:{topic}", data))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def listen(self, topics: list[str]) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(*[f"{self.prefix}:{t}" for t in topics])
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            try:
                payload = json.loads(msg["data"])
            except (TypeError, ValueError):
                continue
            InMemoryBus.publish(self, payload["topic"], payload["data"])
