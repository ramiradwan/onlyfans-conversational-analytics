"""Reuse bounded source catalogs while their verified source token is unchanged."""

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock

from app.analytics.opaque_refs import account_ref
from app.analytics.source_tokens import IDENTITY_LIFETIME_SECONDS, MAX_IDENTITIES

MAX_CATALOG_BYTES = 512 * 1024
MAX_CATALOG_CONVERSATIONS = 4096


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    token: object
    identity: object
    digests: dict[str, str]
    deadline: float
    size: int


class SourceCatalogCache:
    def __init__(self, identities):
        self.identities = identities
        self.entries = OrderedDict()
        self.lock = RLock()
        self.bytes_used = 0

    def _remove(self, key):
        item = self.entries.pop(key, None)
        if item is not None:
            self.bytes_used -= item.size

    def _expire(self):
        now = self.identities._clock()
        for key, item in list(self.entries.items()):
            if now >= item.deadline:
                self._remove(key)

    def get(self, account, token):
        key = account_ref(account)
        with self.lock:
            self._expire()
            item = self.entries.get(key)
            identity = self.identities.get(account, token)
            if item is None:
                return None
            if item.token != token or item.identity != identity:
                self._remove(key)
                return None
            self.entries.move_to_end(key)
            return item.identity, dict(item.digests)

    def put(self, account, token, identity, digests):
        key = account_ref(account)
        with self.lock:
            self._expire()
            self._remove(key)
            if token is None or self.identities.get(account, token) != identity:
                return
            if len(digests) > MAX_CATALOG_CONVERSATIONS:
                return
            size = sum(len(k.encode('utf-8')) + len(v.encode('utf-8')) for k, v in digests.items())
            if size > MAX_CATALOG_BYTES:
                return
            while self.entries and (len(self.entries) >= MAX_IDENTITIES
                    or self.bytes_used + size > MAX_CATALOG_BYTES):
                self._remove(next(iter(self.entries)))
            self.entries[key] = CatalogEntry(token, identity, dict(digests),
                self.identities._clock() + IDENTITY_LIFETIME_SECONDS, size)
            self.bytes_used += size
