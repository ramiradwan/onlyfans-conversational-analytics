"""Bounded currentness proofs for scheduler reconciliation of unchanged content."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Callable
import time

from app.analytics.identity import CanonicalIdentity
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.opaque_refs import account_ref
from app.analytics.validation_receipt import content_stamp, generation_binding

MAX_CURRENTNESS = 8
CURRENTNESS_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class CurrentnessProof:
    snapshot: tuple
    expires_at: float
    source_due_at: datetime | None


class GenerationCurrentness:
    def __init__(self, *, monotonic=time.monotonic):
        self.clock = monotonic
        self.entries: OrderedDict[str, CurrentnessProof] = OrderedDict()
        self.lock = RLock()

    def _snapshot(self, store, account, identity, revision, config):
        with store.database.read() as db:
            db.execute('BEGIN')
            row = db.execute("""SELECT * FROM projection_generations
                WHERE creator_account_id=? AND status='active'""", (account_ref(account),)).fetchone()
            if (row is None or row['canonical_revision'] != identity.revision
                    or row['canonical_content_digest'] != identity.content_digest
                    or row['pipeline_revision'] != revision or row['pipeline_config_digest'] != config):
                return None
            witness = store.activation.get(row['generation_id'])
            if (not store._intent_matches(row, witness, require_completed=True)
                    or witness.creator_account_id != account):
                return None
            stamp = content_stamp(db)
            return row['generation_id'], generation_binding(row), stamp

    def _verified_source_due(self, store, snapshot, cancellation_check=None):
        """Recheck existing complete-content proofs under their original bindings."""
        if snapshot[2] is None:
            return False, None
        envelope_reader = getattr(store, "_trusted_verification_envelope", None)
        methods = tuple(getattr(store, name, None) for name in (
            "_trusted_graph_segment_proof", "_trusted_conversation_graph_proof",
            "_trusted_conversation_enrichment_proof"))
        if not callable(envelope_reader) and not all(callable(method) for method in methods):
            return False, None
        with store.database.read() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM projection_generations WHERE generation_id=?',
                             (snapshot[0],)).fetchone()
            if (row is None or row['status'] != 'active' or row['activated_at'] is None
                    or generation_binding(row) != snapshot[1]):
                return False, None
            observed_stamp = content_stamp(db)
            if observed_stamp != snapshot[2]:
                return False, None
            if callable(envelope_reader):
                envelope = envelope_reader(db, row)
                if envelope is not None and envelope.stamp == snapshot[2]:
                    return True, envelope.source_due_at
            if all(callable(method) for method in methods):
                graph, conversations, enrichment = (method(db, row) for method in methods)
                if (graph is not None and conversations is not None and enrichment is not None
                        and enrichment.stamp == snapshot[2] and enrichment.headers):
                    return True, min(header.expires_at for header in enrichment.headers)
        prepare = getattr(store, "prepare_current_verification_envelope", None)
        if callable(prepare):
            return prepare(snapshot, cancellation_check=cancellation_check)
        return False, None

    def matches(self, store, account: str, identity: CanonicalIdentity, revision: str,
                config: str, retention_clock: Callable[[], datetime], *,
                cancellation_check=None) -> bool:
        """Reuse a checked positive result only while its actual storage stamp matches."""

        snapshot = self._snapshot(store, account, identity, revision, config)
        if snapshot is None:
            return False
        key = account_ref(account)
        with self.lock:
            for expired in [key for key, value in self.entries.items()
                            if self.clock() >= value.expires_at]:
                self.entries.pop(expired, None)
            proof = self.entries.get(key)
            if (proof is not None and snapshot[2] is not None
                    and proof.snapshot == snapshot):
                if proof.source_due_at is not None and proof.source_due_at <= retention_clock():
                    self.entries.pop(key, None)
                    return False
                # Reconciliation runs every 30 seconds for the reference
                # scheduler. Renew a still-bound proof at half-life instead of
                # letting the 60-second expiry force expensive verification
                # immediately before the next interactive mutation.
                if proof.expires_at - self.clock() > CURRENTNESS_SECONDS / 2:
                    self.entries.move_to_end(key)
                    return True
            self.entries.pop(key, None)
        verified, due = self._verified_source_due(store, snapshot, cancellation_check)
        if not verified:
            projection = store.get(account, canonical_identity=identity)
            if (projection is None or projection.account_ref != account_ref(account)
                    or projection.canonical_content_digest != identity.content_digest
                    or projection.source_revision != identity.revision
                    or projection.pipeline_revision != revision
                    or projection.pipeline_config_digest != config):
                return False
            first = min((m.sent_at for m in projection.message_enrichments), default=None)
            due = first + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) if first else None
        if due is not None and due <= retention_clock():
            return False
        after = self._snapshot(store, account, identity, revision, config)
        if after is None or after[:2] != snapshot[:2]:
            return False
        if after == snapshot and snapshot[2] is not None:
            with self.lock:
                self.entries[key] = CurrentnessProof(snapshot, self.clock() + CURRENTNESS_SECONDS, due)
                self.entries.move_to_end(key)
                while len(self.entries) > MAX_CURRENTNESS:
                    self.entries.popitem(last=False)
        return True
