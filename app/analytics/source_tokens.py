"""Bounded process-local digest reuse under transactionally maintained source tokens."""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import re
from threading import RLock
import time
from typing import Callable

from app.analytics.identity import CanonicalIdentity
from app.analytics.opaque_refs import account_ref

TOKEN_SCHEMA_DIGEST = '43d2a812f88198794a60be8b53dace8b3119c3f3c29db6484ef9dfacbc4a4915'
MAX_IDENTITIES = 8
IDENTITY_LIFETIME_SECONDS = 60.0


@dataclass(frozen=True)
class SourceToken:
    schema: int
    value: str
    revision: int


class SourceIdentityCache:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic):
        self._clock = monotonic
        self._lock = RLock()
        self._entries: OrderedDict[str, tuple[SourceToken, CanonicalIdentity, float]] = OrderedDict()
        self._schema: tuple[int, bool] | None = None

    def token(self, connection, account_id: str) -> SourceToken | None:
        schema = int(connection.execute('PRAGMA schema_version').fetchone()[0])
        with self._lock:
            if self._schema is None or self._schema[0] != schema:
                rows = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'analytics_source_token_*'").fetchall()
                values = {row[0]: hashlib.sha256(row[1].encode()).hexdigest() for row in rows}
                digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analytics_source_tokens'").fetchone()
                self._schema = (schema, digest == TOKEN_SCHEMA_DIGEST and exists is not None)
                self._entries.clear()
            valid = self._schema[1]
        if not valid:
            return None
        row = connection.execute('''SELECT t.token,h.canonical_revision FROM analytics_source_tokens t
            JOIN account_heads h ON h.creator_account_id=t.creator_account_id
            WHERE t.creator_account_id=?''', (account_id,)).fetchone()
        if row is None or not isinstance(row[0], str) or not re.fullmatch('[0-9a-f]{32}', row[0]):
            return None
        return SourceToken(schema, row[0], int(row[1]))

    def get(self, account_id: str, token: SourceToken | None) -> CanonicalIdentity | None:
        key = account_ref(account_id)
        with self._lock:
            self._expire()
            entry = self._entries.get(key)
            if entry is None:
                return None
            if token is None or entry[0] != token:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def put(self, account_id: str, token: SourceToken | None, identity: CanonicalIdentity) -> None:
        if token is None or token.revision != identity.revision:
            return
        with self._lock:
            self._expire()
            key = account_ref(account_id)
            self._entries[key] = (token, identity, self._clock() + IDENTITY_LIFETIME_SECONDS)
            self._entries.move_to_end(key)
            while len(self._entries) > MAX_IDENTITIES:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._schema = None

    def _expire(self) -> None:
        now = self._clock()
        for key, (_, _, deadline) in list(self._entries.items()):
            if now >= deadline:
                self._entries.pop(key, None)
