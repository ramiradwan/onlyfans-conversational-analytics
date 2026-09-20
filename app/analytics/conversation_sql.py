"""Read and stage optional conversation fragments in the analytics database."""

import hashlib
from contextlib import contextmanager

from app.analytics.cancellation import check_cancelled
from app.analytics.database import generation_verification_cache
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
    with store.database.read() as db, generation_verification_cache(db):
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
            from app.analytics.shared_graph import supported as shared_graph_supported
            load.graph_reference_pages = (getattr(store, "reuse_graph_content", True)
                and getattr(store, "reuse_graph_page_references", True) and shared_graph_supported(db))
        from app.analytics import conversation_graph_unit_sql as graph_units
        proof_reader = getattr(store, '_trusted_conversation_graph_proof', None)
        graph_unit_proof = (
            proof_reader(db, generation)
            if valid and graph_units.supported(db) and callable(proof_reader)
            else None
        )
        if graph_unit_proof is not None:
            trusted_headers = {
                header.conversation_ref: header for header in graph_unit_proof.headers
            }
            def graph_unit_reference(conversation, input_digest, config_digest):
                value = graph_units.load_reference(
                    db, generation['generation_id'], partition, conversation,
                    input_digest, config_digest,
                )
                return value if (
                    value is not None
                    and trusted_headers.get(conversation) == value.header
                ) else None
            def previous_graph_unit(conversation):
                value = graph_units.load_unit(
                    db, generation['generation_id'], partition, conversation,
                )
                return value if (
                    value is not None
                    and trusted_headers.get(conversation) == value.header
                ) else None
            def graph_unit_references():
                values = graph_units.list_references(
                    db, generation['generation_id'], partition,
                )
                return values if all(
                    trusted_headers.get(item.header.conversation_ref) == item.header
                    for item in values
                ) else ()
            load.graph_unit_reference = graph_unit_reference
            load.previous_graph_unit = previous_graph_unit
            load.graph_unit_references = graph_unit_references
            load.graph_unit_proof = graph_unit_proof
            load.graph_units_supported = True
            load.active_generation_id = generation['generation_id']
        segment_proof_reader = getattr(store, '_trusted_graph_segment_proof', None)
        graph_segment_proof = (
            segment_proof_reader(db, generation)
            if valid and callable(segment_proof_reader) else None
        )
        if graph_segment_proof is not None:
            from app.analytics.shared_graph import (
                selected_content_ids, verified_segment_chunk,
                verified_segment_chunks_complete,
            )
            def graph_content_ids(kind, keys, check=lambda: None):
                return selected_content_ids(
                    db, generation['generation_id'], partition, kind, keys, check
                )
            def graph_segment_chunk(kind, bucket):
                return verified_segment_chunk(
                    db, partition, graph_segment_proof, kind, bucket
                )
            load.graph_segment_proof = graph_segment_proof
            load.graph_content_ids = graph_content_ids
            load.graph_segment_chunk = graph_segment_chunk
            load.graph_chunks_complete = verified_segment_chunks_complete(
                db, partition, graph_segment_proof
            )
        yield load
