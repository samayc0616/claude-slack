"""Session state: thread_ts ↔ session_id ↔ cwd ↔ status, persisted JSON."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .config import SESSIONS_PATH


@dataclass
class Session:
    thread_ts: str
    channel: str
    cwd: str
    session_id: str = ""
    label: str = ""
    status: str = "idle"
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    card_ts: str = ""
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    last_user_id: str = ""    # Slack user who last prompted; target for waiting-DMs
    last_user_msg_ts: str = ""  # ts of the message that became the last prompt


class SessionManager:
    """Async-safe session registry with on-disk persistence and per-session locks."""

    def __init__(self, path: Path = SESSIONS_PATH) -> None:
        self.path = path
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._io_lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return
        for ts, data in raw.items():
            self._sessions[ts] = Session(**data)

    async def _flush(self) -> None:
        async with self._io_lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({ts: asdict(s) for ts, s in self._sessions.items()}, indent=2))
            tmp.replace(self.path)

    def get(self, thread_ts: str) -> Session | None:
        return self._sessions.get(thread_ts)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def lock(self, thread_ts: str) -> asyncio.Lock:
        if thread_ts not in self._locks:
            self._locks[thread_ts] = asyncio.Lock()
        return self._locks[thread_ts]

    async def upsert(self, sess: Session) -> None:
        sess.last_activity = time.time()
        self._sessions[sess.thread_ts] = sess
        await self._flush()

    async def set_status(self, thread_ts: str, status: str) -> None:
        s = self._sessions.get(thread_ts)
        if not s:
            return
        s.status = status
        s.last_activity = time.time()
        await self._flush()

    async def remove(self, thread_ts: str) -> None:
        self._sessions.pop(thread_ts, None)
        self._locks.pop(thread_ts, None)
        await self._flush()
