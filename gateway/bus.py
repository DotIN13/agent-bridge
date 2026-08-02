"""In-memory pub/sub for live SSE fan-out.

Workers publish events keyed by job_id; SSE handlers subscribe. Persistence is
handled separately by the DB, so a subscriber that connects late replays the
backlog from SQLite and then joins the live stream here.
"""
from __future__ import annotations

import queue
import threading
from typing import Any

_SENTINEL = object()


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[queue.SimpleQueue]] = {}

    def subscribe(self, job_id: str) -> queue.SimpleQueue:
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            self._subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id: str, q: queue.SimpleQueue) -> None:
        with self._lock:
            subs = self._subs.get(job_id)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subs.pop(job_id, None)

    def publish(self, job_id: str, item: Any) -> None:
        with self._lock:
            subs = list(self._subs.get(job_id, ()))
        for q in subs:
            q.put(item)

    def close(self, job_id: str) -> None:
        """Signal end-of-stream to all current subscribers of a job."""
        self.publish(job_id, _SENTINEL)


def is_end(item: Any) -> bool:
    return item is _SENTINEL
