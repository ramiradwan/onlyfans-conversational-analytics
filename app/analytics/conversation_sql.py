"""Read and stage optional conversation fragments in the analytics database."""

import hashlib
from contextlib import contextmanager

from app.analytics.cancellation import check_cancelled
from app.analytics.conversation_reuse import MAX_FRAGMENT_BYTES
from app.analytics.opaque_refs import account_ref


def insert_fragments(connection, generation_id, fragments, entries):
    for item, raw in zip(fragments, entries, strict=True):
        data = raw.decode()
        connection.execute("""INSERT INTO conversation_fragments
            (generation_id,creator_account_id,conversation_ref,input_digest,config_digest,
             document_json,document_digest) VALUES (?,?,?,?,?,?,?)""",
            (generation_id, item.account_ref, item.conversation_ref, item.input_digest,
             item.config_digest, data, hashlib.sha256(data.encode()).hexdigest()))


def load_fragment(store, account_id, conversation, input_digest, config_digest, *, cancellation_check=None):
    with fragment_reader(store, account_id) as load:
        return load(conversation, input_digest, config_digest, cancellation_check=cancellation_check)


@contextmanager
def fragment_reader(store, account_id):
    """Share one witnessed predecessor connection across a build's lookups."""

    partition = account_ref(account_id)
    with store.database.read() as db:
        generation = db.execute("""SELECT * FROM projection_generations
            WHERE creator_account_id=? AND status='active' AND activated_at IS NOT NULL""",
            (partition,)).fetchone()
        intent = None if generation is None else store.activation.get(generation['generation_id'])
        valid = (store._intent_matches(generation, intent, require_completed=True)
                 and intent.creator_account_id == account_id)
        def load(conversation, input_digest, config_digest, *, cancellation_check=None):
            check_cancelled(cancellation_check)
            if not valid:
                return None
            row = db.execute("""SELECT document_json,document_digest FROM conversation_fragments
                WHERE generation_id=? AND creator_account_id=? AND conversation_ref=?
                AND input_digest=? AND config_digest=?""",
                (generation['generation_id'], partition, conversation, input_digest, config_digest)).fetchone()
            check_cancelled(cancellation_check)
            if row is None:
                return None
            data = row['document_json'].encode()
            return data if len(data) <= MAX_FRAGMENT_BYTES and hashlib.sha256(data).hexdigest() == row['document_digest'] else None
        from app.analytics.conversation_page_sql import supported, load_pages
        if supported(db):
            def pages(conversation, input_digest, config_digest, *, cancellation_check=None):
                if not valid:
                    return None
                return load_pages(db, generation['generation_id'], partition, conversation,
                    input_digest, config_digest, cancellation_check=cancellation_check)
            load.pages = pages
        yield load
