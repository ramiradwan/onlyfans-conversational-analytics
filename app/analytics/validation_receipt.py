"""Bounded receipts for previously verified generation contents."""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from threading import RLock
import time

TRIGGER_DIGEST = '4f8d41483167efa99224beced8dad86e5354268c95d40cd32340375dea39c260'
TRIGGER_DIGESTS = {12: TRIGGER_DIGEST,
    13: '5fc7f7109392c4a934b6494e03c51bae6e83f36f189252449006ad69e543ea16',
    14: '09cd0953848b1457a16ddf52d9afe76cdf283bb459173baee1be4ac1db3e0a39',
    15: '09cd0953848b1457a16ddf52d9afe76cdf283bb459173baee1be4ac1db3e0a39',
    16: '09cd0953848b1457a16ddf52d9afe76cdf283bb459173baee1be4ac1db3e0a39',
    17: 'ed2aa6ac0d8643ccb76e53781fb019617bba9d7a3310742db59e2611634a437d',
    18: 'ed2aa6ac0d8643ccb76e53781fb019617bba9d7a3310742db59e2611634a437d'}
MAX_RECEIPTS = 8
RECEIPT_SECONDS = 60.0
_VOLATILE = frozenset({'status', 'activation_intent_id', 'witness_sequence',
    'lease_expires_at', 'activated_at', 'retired_at'})


def content_stamp(connection):
    expected = TRIGGER_DIGESTS.get(connection.execute("PRAGMA user_version").fetchone()[0])
    if expected is None:
        return None
    table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_content_epoch'").fetchone()
    if not table:
        return None
    rows = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'generation_content_*'")
    signatures = {row[0]: hashlib.sha256(row[1].encode()).hexdigest() for row in rows}
    encoded = json.dumps(signatures, sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(encoded).hexdigest() != expected:
        return None
    schema = connection.execute('PRAGMA schema_version').fetchone()[0]
    epoch = connection.execute('SELECT value FROM generation_content_epoch WHERE singleton=1').fetchone()
    store = connection.execute('SELECT store_id,schema_identity FROM projection_store_identity WHERE singleton=1').fetchone()
    return (store[0], store[1], schema, epoch[0]) if store and epoch else None


def generation_binding(row):
    values = {key: row[key] for key in row.keys() if key not in _VOLATILE}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationReceipt:
    generation_id: str
    stamp: tuple
    binding: str
    expires_at: float


def capture_receipt(connection, generation_id, *, verified_stamp, monotonic=time.monotonic):
    """Called inside the transaction that independently checked the persisted records."""

    stamp = content_stamp(connection)
    if stamp is None or verified_stamp != stamp:
        return None
    row = connection.execute('SELECT * FROM projection_generations WHERE generation_id=?',
                             (generation_id,)).fetchone()
    if row is None or row['status'] != 'validated':
        return None
    return ValidationReceipt(generation_id, stamp, generation_binding(row),
                             monotonic() + RECEIPT_SECONDS)


class ValidationReceipts:
    def __init__(self, *, monotonic=time.monotonic):
        self.clock = monotonic
        self.entries = OrderedDict()
        self.lock = RLock()

    def put(self, receipt):
        if receipt is None:
            return
        with self.lock:
            self.entries[receipt.generation_id] = receipt
            self.entries.move_to_end(receipt.generation_id)
            while len(self.entries) > MAX_RECEIPTS:
                self.entries.popitem(last=False)

    def take(self, connection, generation):
        """Consume once while the activation transaction excludes competing writers."""

        if not connection.in_transaction:
            raise ValueError('validation_receipt_requires_transaction')
        with self.lock:
            receipt = self.entries.pop(generation['generation_id'], None)
        return bool(receipt is not None and self.clock() < receipt.expires_at
            and receipt.binding == generation_binding(generation)
            and receipt.stamp == content_stamp(connection))
