"""asyncio pub/sub. Publishing is non-blocking; a slow subscriber drops, never stalls."""

import asyncio
import time
import uuid

from .events import Event
from .timing import Clock


class Bus:
    def __init__(self, session_id=None, clock=None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.clock = clock or Clock()
        self.seq = 0
        self._subscribers: list[asyncio.Queue] = []
        self._sinks: list = []

    def subscribe(self, maxsize=512):
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue):
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def add_sink(self, sink):
        """A sink is a callable taking the stamped event; it must not block."""
        self._sinks.append(sink)

    def publish(self, event: Event) -> Event:
        self.seq += 1
        stamped = event.model_copy(
            update={
                "session_id": self.session_id,
                "seq": self.seq,
                "t_wall": time.time(),
                "t_ms": self.clock.t_ms(),
            }
        )
        for sink in self._sinks:
            try:
                sink(stamped)
            except Exception:  # a broken sink must never stop the agent
                pass
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(stamped)
            except asyncio.QueueFull:
                # The status page is allowed to miss frames; the JSONL log is not.
                pass
        return stamped
