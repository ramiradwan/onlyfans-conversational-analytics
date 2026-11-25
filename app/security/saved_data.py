"""Process-local sessions for an enrolled creator's saved workspace."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Callable

from app.persistence.auth import SavedDataOwner

if TYPE_CHECKING:
    from app.security.account_passkey_records import VerifiedPasskeyRecord


@dataclass(frozen=True, slots=True, repr=False)
class InitialSavedDataAccess:
    """Verified enrollment authority held by the process that completed setup."""

    generation: str
    owner: SavedDataOwner
    record: VerifiedPasskeyRecord


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
        self._initial: InitialSavedDataAccess | None = None

    def stage_initial(self, access: InitialSavedDataAccess) -> None:
        with self._lock:
            if self._initial is not None:
                raise RuntimeError("Initial saved-data access was already staged")
            self._initial = access

    def has_initial(self, generation: str) -> bool:
        with self._lock:
            return self._initial is not None and self._initial.generation == generation

    def issue_initial(
        self, generation: str, owner: SavedDataOwner, ttl_seconds: int,
        *, verify: Callable[[VerifiedPasskeyRecord], bool],
    ) -> tuple[str, str] | None:
        """Retire the exact proof and issue its session under the same lock."""
        with self._lock:
            pending = self._initial
            if pending is None or pending.generation != generation:
                return None
            self._initial = None
            if pending.owner != owner or not verify(pending.record):
                return None
            return self._issue(owner, ttl_seconds)

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

    def invalidate(self, owner: SavedDataOwner) -> None:
        with self._lock:
            if self._initial is not None and self._initial.owner == owner:
                self._initial = None
            self._sessions = {token: session for token, session in self._sessions.items() if session.owner != owner}
