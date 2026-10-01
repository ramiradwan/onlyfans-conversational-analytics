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
        reader_open = [True]
        loaded_enrichment_unit = [None]
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
        load.integrity_checksum_version = (2 if db.execute("PRAGMA user_version").fetchone()[0] >= 21
            and getattr(store, "reuse_graph_content", True) else 1)
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
                    and value.header.checksum_version == load.integrity_checksum_version
                ) else None
            loaded_graph_unit = [None]
            def previous_graph_unit(conversation):
                value = graph_units.load_unit(
                    db, generation['generation_id'], partition, conversation,
                )
                value = value if (
                    value is not None
                    and trusted_headers.get(conversation) == value.header
                ) else None
                # Keep only the latest actual unit. A caller-created unit with
                # a copied header cannot obtain construction reuse authority.
                loaded_graph_unit[0] = value
                return value
            def insertion_graph_groups(unit, check=lambda: None):
                from app.analytics.conversation_id_frames import checked_predecessor_groups
                if (not reader_open[0] or unit is None or unit is not loaded_graph_unit[0]
                        or trusted_headers.get(unit.header.conversation_ref) != unit.header):
                    return None
                return checked_predecessor_groups(unit, check)
            load.insertion_graph_groups = insertion_graph_groups
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
            from app.analytics import conversation_enrichment_unit_sql as enrichment_units
            enrichment_proof_reader = getattr(
                store, '_trusted_conversation_enrichment_proof', None
            )
            enrichment_proof = (
                enrichment_proof_reader(db, generation)
                if enrichment_units.supported(db)
                and callable(enrichment_proof_reader) else None
            )
            if enrichment_proof is not None:
                enrichment_headers = {
                    header.conversation_ref: header
                    for header in enrichment_proof.headers
                }
                def enrichment_unit_reference(conversation, input_digest, config_digest):
                    value = enrichment_units.load_reference(
                        db, generation['generation_id'], partition, conversation,
                        input_digest, config_digest,
                    )
                    return value if (
                        value is not None
                        and enrichment_headers.get(conversation) == value.header
                    ) else None
                load.enrichment_unit_reference = enrichment_unit_reference
                def previous_enrichment_unit(conversation):
                    # A failed next read must not leave the preceding selection
                    # authorized by this reader's one-unit identity binding.
                    loaded_enrichment_unit[0] = None
                    if not reader_open[0]:
                        return None
                    unit = enrichment_units.load_unit(db, generation['generation_id'], partition, conversation)
                    if unit is None or enrichment_headers.get(conversation) != unit.header:
                        return None
                    loaded_enrichment_unit[0] = unit
                    return unit
                def matched_enrichment_source(raw, unit, check):
                    if (not reader_open[0] or unit is None or unit is not loaded_enrichment_unit[0]
                            or enrichment_headers.get(unit.header.conversation_ref) != unit.header):
                        return None
                    from app.analytics.conversation_enrichment_units import message_records
                    from app.analytics.conversation_insertion import match_proven_inserted_source
                    rows = message_records(unit)
                    matched = match_proven_inserted_source(account_id, raw, unit.header, rows, check)
                    return None if matched is None else (rows, matched)
                load.previous_enrichment_unit = previous_enrichment_unit
                load.matched_enrichment_source = matched_enrichment_source
                from app.analytics.conversation_page_sql import load_page_header
                from app.analytics.conversation_pages import trusted_page_reference
                def enrichment_page_reference(conversation, input_digest, config_digest):
                    header = load_page_header(
                        db, generation['generation_id'], partition, conversation,
                        input_digest, config_digest,
                    )
                    return (
                        None if header is None else
                        trusted_page_reference(
                            generation['generation_id'], header,
                            enrichment_proof.stamp,
                        )
                    )
                load.enrichment_page_reference = enrichment_page_reference
                load.enrichment_unit_proof = enrichment_proof
                load.enrichment_units_supported = True
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
            from app.analytics.graph_membership_pages import supported as pages_supported
            page_layout = pages_supported(db)
            chunk_cache = {}
            def graph_segment_chunk(kind, bucket):
                if not reader_open[0]:
                    raise ValueError('conversation_reader_closed')
                key = (kind, bucket)
                if key not in chunk_cache:
                    chunk_cache[key] = verified_segment_chunk(
                        db, partition, graph_segment_proof, kind, bucket
                    )
                return chunk_cache[key]
            def graph_content_ids(kind, keys, check=lambda: None):
                # Generic verification retains the independently
                # selected persisted-content lookup.
                return selected_content_ids(
                    db, generation['generation_id'], partition, kind, keys, check,
                    page_layout=page_layout,
                )
            def append_graph_content_ids(kind, keys, check=lambda: None):
                # Admitted append/insertion construction consumes canonical chunks that
                # were just matched to the live predecessor segment proof.
                if kind not in ('node', 'edge'):
                    raise ValueError('graph_record_kind_invalid')
                from collections import defaultdict
                from app.analytics.conversation_append import (
                    verified_chunk_content_ids,
                )
                grouped = defaultdict(list)
                for key in dict.fromkeys(keys):
                    grouped[key[3:5]].append(key)
                result = {}
                for bucket, selected in sorted(grouped.items()):
                    check()
                    opened = graph_segment_chunk(kind, bucket)
                    if opened is None:
                        return {}
                    segment, encoded = opened
                    result.update(verified_chunk_content_ids(
                        segment, encoded, partition, selected, check
                    ))
                return result
            def insertion_graph_content_ids(kind, keys, check=lambda: None):
                # A small conversation must not read an account-sized chunk just
                # to resolve a handful of members. Keep the existing point path.
                unit = loaded_graph_unit[0] if graph_unit_proof is not None else None
                count = 0 if unit is None else unit.header.node_count + unit.header.edge_count
                total = sum(segment.count for segment in graph_segment_proof.segments)
                if unit is not None and 4 * count >= total:
                    return append_graph_content_ids(kind, keys, check)
                return graph_content_ids(kind, keys, check)
            load.insertion_graph_content_ids = insertion_graph_content_ids
            load.graph_segment_proof = graph_segment_proof
            load.graph_content_ids = graph_content_ids
            load.append_graph_content_ids = append_graph_content_ids
            load.graph_segment_chunk = graph_segment_chunk
            load.graph_chunks_complete = verified_segment_chunks_complete(
                db, partition, graph_segment_proof
            )
        try:
            yield load
        finally:
            reader_open[0] = False
            loaded_enrichment_unit[0] = None
            if graph_unit_proof is not None:
                loaded_graph_unit[0] = None
            if graph_segment_proof is not None:
                chunk_cache.clear()
