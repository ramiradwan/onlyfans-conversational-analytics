"""Process-local sessions for an enrolled creator's saved workspace."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from app.persistence.auth import SavedDataOwner

@dataclass(frozen=True, slots=True)
class _Session:
    owner: SavedDataOwner
    csrf_digest: bytes
    deadline: float


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


class SavedDataSessions:
    """A process restart or a fixed deadline requires another Hello assertion."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._sessions: dict[bytes, _Session] = {}

    def issue(self, owner: SavedDataOwner, ttl_seconds: int) -> tuple[str, str]:
        with self._lock:
            return self._issue(owner, ttl_seconds)

    def _issue(self, owner: SavedDataOwner, ttl_seconds: int) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self._clock()
        self._sessions = {key: s for key, s in self._sessions.items() if now < s.deadline}
        self._sessions[_digest(token)] = _Session(owner, _digest(csrf), now + ttl_seconds)
        return token, csrf

    def accepts(self, owner: SavedDataOwner, token: str, csrf: str) -> bool:
        with self._lock:
            session = self._sessions.get(_digest(token))
            return bool(session and self._clock() < session.deadline and session.owner == owner
                        and hmac.compare_digest(session.csrf_digest, _digest(csrf)))

    def close(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_digest(token), None)

    def lock_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def is_open(self, token: str) -> bool:
        with self._lock:
            session = self._sessions.get(_digest(token))
            return session is not None and self._clock() < session.deadline

    def invalidate(self, owner: SavedDataOwner) -> None:
        with self._lock:
            self._sessions = {token: session for token, session in self._sessions.items() if session.owner != owner}
