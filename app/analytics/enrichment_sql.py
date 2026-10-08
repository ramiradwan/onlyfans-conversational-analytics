"""Generation-scoped enrichment records in the encrypted analytics store."""

import hashlib
from datetime import timezone
from itertools import islice

from app.analytics.cancellation import check_cancelled
from app.analytics.enrichment_cache import MAX_ENTRY_BYTES
from app.analytics.opaque_refs import account_ref


INSERT_BATCH_SIZE = 64


def insert_entries(connection, generation_id, entries, *, check=lambda: None, shared=False):
    """Write checked scalar records in bounded batches within the caller's transaction."""

    if shared and not connection.in_transaction:
        raise ValueError("enrichment_sharing_requires_transaction")
    rows = iter(entries)
    while True:
        check()
        batch = list(islice(rows, INSERT_BATCH_SIZE))
        if not batch:
            return
        check()
        if shared:
            _insert_shared_batch(connection, generation_id, batch, check)
            continue
        connection.executemany(
            """INSERT INTO enrichment_reuse
               (generation_id,creator_account_id,cache_key,expires_at,document_json,document_digest)
               VALUES (?,?,?,?,?,?)""",
            [(generation_id, entry.account_ref, entry.cache_key, entry.expires_at,
              entry.document_json, entry.document_digest) for entry in batch],
        )
        check()


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


def shared_entries_supported(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_refs'").fetchone() is not None


def _insert_shared_batch(connection, generation_id, batch, check):
    """Compare stored bytes before sharing; keep damaged optional content untouched."""

    present = {}
    for account in dict.fromkeys(entry.account_ref for entry in batch):
        check()
        identities = list(dict.fromkeys(entry.document_digest for entry in batch if entry.account_ref == account))
        marks = ','.join('?' for _ in identities)
        rows = connection.execute(
            '''SELECT content_id,CASE WHEN typeof(document_json)='text'
                AND length(CAST(document_json AS BLOB))<=? THEN document_json END AS document_json
                FROM enrichment_content WHERE creator_account_id=? AND content_id IN (''' + marks + ')',
            (MAX_ENTRY_BYTES, account, *identities))
        try:
            for row in rows:
                check()
                present[account, row['content_id']] = row['document_json']
        finally:
            rows.close()
    content, references, owned = [], [], []
    for entry in batch:
        check()
        key = entry.account_ref, entry.document_digest
        previous = present.get(key)
        if key in present and previous != entry.document_json:
            owned.append((generation_id, entry.account_ref, entry.cache_key, entry.expires_at,
                          entry.document_json, entry.document_digest))
            continue
        if key not in present:
            content.append((*key, entry.document_json))
            present[key] = entry.document_json
        references.append((generation_id, entry.account_ref, entry.cache_key, entry.expires_at, entry.document_digest))
    for statement, parameters in (
        ('INSERT INTO enrichment_content(creator_account_id,content_id,document_json) VALUES (?,?,?)', content),
        ('INSERT INTO enrichment_refs(generation_id,creator_account_id,cache_key,expires_at,content_id) VALUES (?,?,?,?,?)', references),
        ('INSERT INTO enrichment_owned_records(generation_id,creator_account_id,cache_key,expires_at,document_json,document_digest) VALUES (?,?,?,?,?,?)', owned),
    ):
        check()
        if parameters:
            connection.executemany(statement, parameters)
    check()
