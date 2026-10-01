"""Prepare reusable metadata from independently checked source and stored bytes."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import time

from app.analytics.cancellation import check_cancelled
from app.analytics.compact_graph import CompactGraph
from app.analytics.conversation_graph_units import create_graph_unit, graph_unit_ids
from app.analytics.conversation_pages import PAGE_RECORDS
from app.analytics.enrichment_cache import fingerprint
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.metrics import build_conversation_metrics, CONVERSATION_METRICS_PROVENANCE
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref, participant_ref
from app.canonical.read_models import AccountReadModel


def expected_units(pipeline, account, catalog, projection, references, cancellation):
    """Reconstruct graph facts, never run classifiers during read preparation."""
    by_conversation = defaultdict(list)
    for item in projection.message_enrichments:
        by_conversation[item.conversation_ref].append(item)
    metrics = {item.conversation_ref: item for item in projection.conversation_metrics}
    chats = {conversation_ref(account, chat): chat for chat in catalog.digests}
    config = fingerprint({'pipeline': pipeline.pipeline_config_digest,
        'metrics': CONVERSATION_METRICS_PROVENANCE.model_dump(mode='json'), 'fragment': 'v1'})
    now = pipeline._retention_clock()
    cutoff = now - timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
    if set(metrics) != {ref.header.conversation_ref for ref in references}:
        raise ValueError('recovered_reuse_conversation_set_invalid')
    check = lambda: check_cancelled(cancellation)
    for reference in references:
        check()
        h = reference.header
        chat = chats.get(h.conversation_ref)
        if (chat is None or h.account_ref != projection.account_ref
                or h.input_digest != catalog.digests[chat] or h.config_digest != config
                or h.retention_cutoff > cutoff or h.expires_at <= now):
            raise ValueError('recovered_reuse_source_binding_invalid')
        raw = catalog.conversation(chat)
        conversation = pipeline._canonical_conversations_inner(
            AccountReadModel(view_revision=catalog.view_revision, conversations={chat: raw}),
            cancellation_check=cancellation)[0]
        messages = [m for m in conversation.messages if m.sent_at > cutoff]
        conversation = conversation.model_copy(update={'messages': messages,
            'last_message_at': messages[-1].sent_at if messages else None})
        findings = sorted(by_conversation[h.conversation_ref], key=lambda m: (m.sent_at, m.source_ordinal))
        if len(findings) != len(messages):
            raise ValueError('recovered_reuse_message_count_invalid')
        participant = participant_ref(account, conversation.platform_user_id)
        for finding, source in zip(findings, messages, strict=True):
            check()
            if (finding.message_ref != message_ref(account, chat, source.message_id)
                    or finding.participant_ref != participant or finding.direction != source.direction
                    or finding.sent_at != source.sent_at or finding.source_ordinal != source.source_ordinal):
                raise ValueError('recovered_reuse_message_binding_invalid')
        expected_metrics = build_conversation_metrics(account, conversation, findings)
        if expected_metrics != metrics[h.conversation_ref]:
            raise ValueError('recovered_reuse_metrics_invalid')
        graph = CompactGraph(projection.account_ref)
        for nodes, edges in pipeline.graph_projector.batches(account, catalog.view_revision,
                [conversation], findings, [expected_metrics], cancellation_check=cancellation):
            graph.add(nodes, edges, check=check)
        unit = create_graph_unit(account_ref=projection.account_ref,
            conversation_ref=h.conversation_ref, input_digest=h.input_digest,
            config_digest=config, cutoff=h.retention_cutoff, findings=findings,
            metrics=expected_metrics, graph=graph, checksum_version=h.checksum_version, check=check)
        if unit is None or unit.header != h:
            raise ValueError('recovered_reuse_graph_header_invalid')
        yield unit, graph


def restore(store, account, catalog, build_expected, check, source_current):
    """Install metadata only after one stable, witnessed read passes every check."""
    from app.analytics import conversation_graph_unit_sql as units
    from app.analytics.conversation_graph_sql import encoded_graph_records
    from app.analytics.conversation_reuse import MAX_GRAPH_UNITS, MAX_GRAPH_UNIT_TOTAL_BYTES
    from app.analytics.database import generation_verification_cache
    from app.analytics.source_snapshot import cancellable_source_read
    from app.analytics.sqlite_projection_store import recompute_generation, ProjectionValidationError
    from app.analytics.validation_receipt import content_stamp, generation_binding, ValidationReceipt, RECEIPT_SECONDS

    with (store.database.read() as db, generation_verification_cache(db),
          cancellable_source_read(db, lambda: check() or False)):
        db.execute('BEGIN')
        check()
        row = db.execute("SELECT * FROM projection_generations WHERE creator_account_id=? "
            "AND status='active'", (account_ref(account),)).fetchone()
        if row is None or not units.supported(db):
            return None
        from app.analytics.shared_graph import uses_segments
        if (not getattr(store, 'reuse_graph_content', True)
                or not uses_segments(db, row['generation_id'], row['creator_account_id'])):
            return None  # Ordinary currentness still verifies the complete fallback graph.
        intent = store.activation.get(row['generation_id'])
        if (not store._intent_matches(row, intent, require_completed=True)
                or intent.creator_account_id != account
                or row['canonical_revision'] != catalog.identity.revision
                or row['canonical_content_digest'] != catalog.identity.content_digest):
            return False
        stamp = content_stamp(db)
        if stamp is None:
            return None
        if (store._trusted_graph_segment_proof(db, row) is not None
                and store._trusted_conversation_graph_proof(db, row) is not None
                and store._trusted_conversation_enrichment_proof(db, row) is not None):
            return True
        count = db.execute('SELECT COUNT(*) FROM conversation_graph_refs '
            'WHERE generation_id=? AND creator_account_id=?',
            (row['generation_id'], row['creator_account_id'])).fetchone()[0]
        if not 0 < count <= MAX_GRAPH_UNITS:
            return None
        values = recompute_generation(db, row['generation_id'], check=check,
            materialize_projection=False, materialize_graph=False)
        from app.analytics.sqlite_projection_store import verify_generation_values
        verify_generation_values(row, values)
        projection = values['projection']
        enrichment_headers = tuple(values.get('enrichment_units', ()))
        integrity = values.get('conversation_integrity')
        expected = {h.conversation_ref for h in enrichment_headers}
        verified_headers = () if integrity is None else tuple(integrity.headers)
        verified_refs = {h.conversation_ref for h in verified_headers}

        # Modern v2 units are already independently checked by recompute_generation.
        # Reuse that exact result to rebuild process-local proof metadata instead of
        # reconstructing every conversation graph from canonical source a second time.
        direct = bool(expected) and verified_refs == expected and len(verified_headers) == len(expected)
        if direct:
            check()
            db.rollback()
            current = db.execute('SELECT * FROM projection_generations WHERE generation_id=?',
                                 (row['generation_id'],)).fetchone()
            if current is None or dict(current) != dict(row) or content_stamp(db) != stamp:
                return False
            witness = store.activation.get(row['generation_id'])
            source_due_at = min(h.expires_at for h in enrichment_headers)
            if (not store._intent_matches(current, witness, require_completed=True)
                    or witness.creator_account_id != account
                    or not source_current(projection, source_due_at=source_due_at)):
                return False
            check()
            receipt = ValidationReceipt(row['generation_id'], stamp,
                generation_binding(row), time.monotonic() + RECEIPT_SECONDS)
            store._remember_graph_segment_proof(receipt, values['graph_segments'])
            store._remember_verified_conversation_graph_proof(receipt, integrity, expected)
            store._remember_conversation_enrichment_proof(receipt, enrichment_headers)
            return store._remember_verification_envelope(receipt) is not None

        # Compatibility fallback for legacy/incomplete optional integrity units.
        values = recompute_generation(db, row['generation_id'], check=check,
            materialize_projection=True, materialize_graph=False)
        verify_generation_values(row, values)
        projection = values['projection']
        references = units.list_references(db, row['generation_id'], row['creator_account_id'])
        headers = {r.header.conversation_ref: r.header for r in references}
        if set(headers) != {h.conversation_ref for h in values.get('enrichment_units', ())}:
            return None
        verified_units, used = [], 0
        for unit, graph in build_expected(projection, references):
            check()
            saved = units.load_unit(db, row['generation_id'], row['creator_account_id'],
                                    unit.header.conversation_ref)
            if saved is None or saved.header != unit.header or graph_unit_ids(saved) != graph_unit_ids(unit):
                raise ProjectionValidationError('recovered_reuse_membership_invalid')
            used += unit.retained_bytes
            if used > MAX_GRAPH_UNIT_TOTAL_BYTES:
                return None
            for kind, records in (('node', graph.nodes), ('edge', graph.edges)):
                keys = sorted(records)
                for offset in range(0, len(keys), PAGE_RECORDS):
                    for actual in encoded_graph_records(db, row['generation_id'], row['creator_account_id'],
                                                        kind, keys[offset:offset + PAGE_RECORDS], check):
                        if actual.data != records[actual.key]:
                            raise ProjectionValidationError('recovered_reuse_graph_content_invalid')
            verified_units.append(unit)
        if len(verified_units) != len(references):
            return False
        check()
        db.rollback()
        current = db.execute('SELECT * FROM projection_generations WHERE generation_id=?',
                             (row['generation_id'],)).fetchone()
        if current is None or dict(current) != dict(row) or content_stamp(db) != stamp:
            return False
        witness = store.activation.get(row['generation_id'])
        if (not store._intent_matches(current, witness, require_completed=True)
                or witness.creator_account_id != account or not source_current(projection)):
            return False
        check()
        receipt = ValidationReceipt(row['generation_id'], stamp,
            generation_binding(row), time.monotonic() + RECEIPT_SECONDS)
        store._remember_graph_segment_proof(receipt, values['graph_segments'])
        store._remember_conversation_graph_proof(receipt, verified_units, None, projection)
        store._remember_conversation_enrichment_proof(receipt, values['enrichment_units'])
        store._remember_verification_envelope(receipt)
        return True
