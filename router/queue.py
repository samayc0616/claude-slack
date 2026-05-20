"""Per-user offline event queue with TTL + max-depth cap."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class QueuedEvent:
    enqueued_at: float
    payload: dict


class EventQueue:
    def __init__(self, ttl_seconds: int, max_per_user: int) -> None:
        self.ttl = ttl_seconds
        self.max = max_per_user
        self._queues: dict[str, deque[QueuedEvent]] = {}

    def enqueue(self, user_id: str, payload: dict) -> None:
        q = self._queues.setdefault(user_id, deque(maxlen=self.max))
        q.append(QueuedEvent(enqueued_at=time.time(), payload=payload))

    def drain(self, user_id: str) -> list[dict]:
        q = self._queues.get(user_id)
        if not q:
            return []
        cutoff = time.time() - self.ttl
        out: list[dict] = []
        while q:
            ev = q.popleft()
            if ev.enqueued_at >= cutoff:
                out.append(ev.payload)
        if not q:
            self._queues.pop(user_id, None)
        return out

    def depth(self, user_id: str) -> int:
        return len(self._queues.get(user_id) or [])
