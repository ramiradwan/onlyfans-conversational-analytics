"""Bounded one-use browser entries for an already prepared reissue."""
from __future__ import annotations

import re
import secrets
import threading
import time
from urllib.parse import urlsplit


class ReissueNavigationEntries:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._entries = {}
        self._lock = threading.Lock()

    def issue(self, owner: tuple[str, str], url: str) -> str:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or re.fullmatch(r'/v2/onboarding/text-analysis-license-reissues/[A-Za-z0-9_-]{43}', parsed.path) is None
                or len(url.encode('utf-8')) > 2048):
            raise ValueError('Reissue navigation is unavailable')
        with self._lock:
            now = self._clock()
            self._entries = {key: value for key, value in self._entries.items()
                             if value[0] > now and value[1] != owner}
            if len(self._entries) >= 64:
                raise ValueError('Reissue navigation capacity is exhausted')
            code = secrets.token_urlsafe(32)
            self._entries[code] = (now + 30, owner, url)
            return code

    def consume(self, code: str) -> str:
        if type(code) is not str or re.fullmatch(r'[A-Za-z0-9_-]{43}', code) is None:
            raise ValueError('Reissue entry is invalid')
        with self._lock:
            value = self._entries.pop(code, None)
            if value is None or value[0] <= self._clock():
                raise ValueError('Reissue entry expired or was already used')
            return value[2]
