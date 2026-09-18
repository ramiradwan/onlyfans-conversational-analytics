"""Generation-scoped enrichment records in the encrypted analytics store."""

import hashlib
from datetime import timezone

from app.analytics.cancellation import check_cancelled
from app.analytics.enrichment_cache import MAX_ENTRY_BYTES
from app.analytics.opaque_refs import account_ref


def insert_entries(connection, generation_id, entries):
    for entry in entries:
        data = entry.model_dump_json()
        connection.execute(
            """INSERT INTO enrichment_reuse
               (generation_id,creator_account_id,cache_key,expires_at,document_json,document_digest)
               VALUES (?,?,?,?,?,?)""",
            (generation_id, entry.key.account_ref, entry.key.digest,
             entry.key.expires_at.isoformat(), data, hashlib.sha256(data.encode()).hexdigest()),
        )


def load_entries(store, account_id, keys, *, now, cancellation_check=None):
    """Reuse only a witnessed predecessor; never expose it as a current projection."""

    if len(keys) > 192:
        raise ValueError("enrichment_lookup_batch_invalid")
    check_cancelled(cancellation_check)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("enrichment_time_requires_timezone")
    now = now.astimezone(timezone.utc)
    partition = account_ref(account_id)
    with store.database.read() as db:
        generation = db.execute(
            """SELECT * FROM projection_generations
               WHERE creator_account_id=? AND status='active'
                 AND activated_at IS NOT NULL
               ORDER BY witness_sequence DESC LIMIT 1""", (partition,),
        ).fetchone()
        if generation is None:
            return {}
        intent = store.activation.get(generation["generation_id"])
        if (not store._intent_matches(generation, intent, require_completed=True)
                or intent.creator_account_id != account_id):
            return {}
        result = {}
        for offset in range(0, len(keys), 64):
            check_cancelled(cancellation_check)
            batch = keys[offset:offset + 64]
            marks = ','.join('?' for _ in batch)
            rows = db.execute(
                """SELECT cache_key,document_json,document_digest FROM enrichment_reuse
                   WHERE generation_id=? AND creator_account_id=? AND expires_at>?
                     AND cache_key IN (""" + marks + ")",
                (generation["generation_id"], partition, now.isoformat(), *batch),
            )
            for row in rows:
                check_cancelled(cancellation_check)
                data = row["document_json"].encode("utf-8")
                if len(data) <= MAX_ENTRY_BYTES and hashlib.sha256(data).hexdigest() == row["document_digest"]:
                    result[row["cache_key"]] = data
        return result
