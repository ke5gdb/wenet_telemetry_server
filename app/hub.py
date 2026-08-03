"""Fan-out of live telemetry to connected browsers.

Ingest runs in a worker thread (SQLite is synchronous) while subscribers live on
the event loop, so publishing has to cross that boundary explicitly.

A subscriber that stops reading -- a laptop that slept, a phone that lost
signal -- must never be able to slow ingest down. Each connection gets a bounded
queue, and when it fills, its oldest event is discarded. For a live display the
newest data is the data that matters, so dropping the stale end is the right
sacrifice.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, queue_size: int = 64):
        self.queue_size = queue_size
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish_threadsafe(self, event: dict) -> None:
        """Publish from a worker thread."""
        loop = self._loop
        if loop is None or not self._subscribers:
            return
        try:
            loop.call_soon_threadsafe(self.publish, event)
        except RuntimeError:
            pass  # loop already closed, during shutdown

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()      # discard the oldest
                    queue.put_nowait(event)
                    self.dropped += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
