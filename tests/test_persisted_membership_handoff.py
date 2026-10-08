"""Count persisted membership restoration over the full build/publication path."""
from contextvars import ContextVar
from time import perf_counter
import json
import pytest
from app.analytics.opaque_refs import conversation_ref
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message
from tests.test_dominant_append_reuse import dominant_fixture

pytestmark = [pytest.mark.ci_tier("integration")]


@pytest.mark.parametrize('state', ['unchanged', 'small', 'dominant', 'idle', 'rebuilt', 'restarted'])
def test_unchanged_units_do_not_restore_membership_models(tmp_path, monkeypatch, record_property, state):
    from app.analytics import conversation_integrity_store as integrity
    from app.analytics import conversation_graph_unit_sql as units
    from app.analytics.validation_receipt import ValidationReceipts
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        if state == 'idle':
            f.stores.projections._currentness.entries.clear()
            assert f.pipeline.prepare_questions(ACCOUNT, 1)
        if state == 'rebuilt':
            f.pipeline.publish_candidate(f.pipeline.build_candidate(ACCOUNT, force=True))
        if state == 'restarted':
            from app.analytics.factory import create_analytics_stores
            from app.analytics.pipeline import AnalyticsPipeline
            f.stores.projections.close_retention_scheduler()
            f.stores = create_analytics_stores('sqlite', projections_path=f.stores.database.path,
                activation=f.repositories.projection_activation, canonical_identity_reader=f.source.read_identity,
                retention_clock=lambda: NOW)
            f.pipeline = AnalyticsPipeline(f.source, projections=f.stores.projections,
                enrichment=f.pipeline.enrichment, clock=lambda: NOW)
            assert f.pipeline.prepare_questions(ACCOUNT, 1)
        active = ContextVar('integrity_validation_active', default=None)
        counts = {'restored_units': [], 'membership_records': 0, 'integrity_seconds': 0.0,
                  'unit_reads': 0, 'unit_payload_bytes': 0, 'identity_scan_bytes': 0, 'identity_scan_units': 0, 'full_read_membership_records': 0, 'verification_calls': [], 'activation_receipt_hits': 0, 'activation_receipt_misses': 0}
        original = integrity.verify_generation_integrity
        def verify(*args, **kwargs):
            counts['verification_calls'].append({'status': args[1]['status'], 'proof': kwargs.get('proof') is not None, 'graph_proof': getattr(kwargs.get('graph_validation'), 'proof', None) is not None})
            token = active.set('staged' if args[1]['status'] == 'building' else 'full')
            started = perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                counts['integrity_seconds'] += perf_counter() - started
                active.reset(token)
        restore = integrity.groups_for_members
        def members(unit, *args, **kwargs):
            if active.get() == 'staged':
                counts['restored_units'].append(unit.header.conversation_ref)
                counts['membership_records'] += unit.header.node_count + unit.header.edge_count
            elif active.get() == 'full':
                counts['full_read_membership_records'] += unit.header.node_count + unit.header.edge_count
            return restore(unit, *args, **kwargs)
        load = units.load_unit
        def read(*args, **kwargs):
            if active.get() == 'staged': counts['unit_reads'] += 1
            value = load(*args, **kwargs)
            if active.get() == 'staged' and value is not None:
                counts['unit_payload_bytes'] += value.retained_bytes
            return value
        from app.analytics import conversation_membership_validation as selection
        scan = selection.contains_changed_member
        def scan_members(data, *args, **kwargs):
            if active.get() == 'staged':
                counts['identity_scan_units'] += 1
                counts['identity_scan_bytes'] += len(data)
            return scan(data, *args, **kwargs)
        monkeypatch.setattr(selection, 'contains_changed_member', scan_members)
        take = ValidationReceipts.take
        def receipt(*args, **kwargs):
            result = take(*args, **kwargs)
            counts['activation_receipt_hits' if result else 'activation_receipt_misses'] += 1
            return result
        monkeypatch.setattr(integrity, 'verify_generation_integrity', verify)
        monkeypatch.setattr(integrity, 'groups_for_members', members)
        monkeypatch.setattr(units, 'load_unit', read)
        monkeypatch.setattr(ValidationReceipts, 'take', receipt)
        changed_chat = 'chat-0' if state == 'dominant' else 'chat-1'
        if state != 'unchanged':
            with f.repositories.database.transaction() as db:
                insert_message(db, changed_chat, 'validated-tail', NOW, 2)
                advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT, force=state == 'unchanged')
        f.pipeline.publish_candidate(candidate)
        record_property('validation_work', json.dumps(counts, sort_keys=True))
        expected = set() if state == 'unchanged' else {conversation_ref(ACCOUNT, changed_chat)}
        assert set(counts['restored_units']) <= expected, counts
        assert counts['unit_reads'] == (0 if state == 'unchanged' else 1), counts
        assert counts['activation_receipt_hits'] == 1, counts
        if state == 'unchanged':
            assert counts['full_read_membership_records'] > 0, 'explicit artifact reads must retain complete verification'
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


@pytest.mark.parametrize('fallback', ['missing_unit_proof', 'missing_segment_proof', 'missing_changed_set', 'small_buffer'])
def test_unavailable_authority_or_capacity_retains_complete_validation(tmp_path, monkeypatch, fallback):
    from unittest.mock import Mock
    from app.analytics import conversation_integrity_store as integrity
    from app.analytics import shared_graph
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original = integrity.verify_generation_integrity
        def verify(*args, **kwargs):
            if fallback == 'missing_unit_proof': kwargs['proof'] = None
            if fallback == 'missing_segment_proof': kwargs['graph_validation'] = None
            if fallback == 'missing_changed_set': kwargs['verified_changes'] = None
            return original(*args, **kwargs)
        monkeypatch.setattr(integrity, 'verify_generation_integrity', verify)
        if fallback == 'small_buffer':
            monkeypatch.setattr(shared_graph, 'MAX_CHANGED_VERIFICATION_ROWS', 1)
        restored = Mock(wraps=integrity.groups_for_members)
        monkeypatch.setattr(integrity, 'groups_for_members', restored)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'fallback-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        assert len({call.args[0].header.conversation_ref for call in restored.call_args_list}) == 3
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


def test_old_unit_reference_cannot_hide_changed_member_content(tmp_path, monkeypatch):
    from app.analytics import conversation_graph_unit_sql as units
    from app.analytics.conversation_graph_units import ConversationGraphReference
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        before = f.stores.database.active_generation(ACCOUNT).generation_id
        chat = conversation_ref(ACCOUNT, 'chat-1')
        with f.stores.projections.open_conversation_fragments(ACCOUNT) as load:
            old = load.previous_graph_unit(chat)
            reference = ConversationGraphReference(load.active_generation_id, old.header)
        original = units.insert_units
        def insert(connection, generation, values, **kwargs):
            values = tuple(reference if value.header.conversation_ref == chat else value for value in values)
            return original(connection, generation, values, **kwargs)
        monkeypatch.setattr(units, 'insert_units', insert)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'changed-but-stale-unit', NOW, 2)
            advance(db)
        with pytest.raises(ValueError, match='conversation_integrity_selected_content_changed'):
            f.pipeline.build_candidate(ACCOUNT)
        assert f.stores.database.active_generation(ACCOUNT).generation_id == before
    finally:
        cleanup(f)



def test_integrity_summary_reader_does_not_load_membership_payloads(tmp_path):
    from app.analytics import conversation_graph_unit_sql as units
    from app.analytics.conversation_integrity import decode_manifest
    from app.analytics.opaque_refs import account_ref
    from tests.continuous_analytics_fixture import make_fixture
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        generation = f.stores.database.active_generation(ACCOUNT).generation_id
        account = account_ref(ACCOUNT)
        chat = conversation_ref(ACCOUNT, 'chat-0')
        with f.stores.database.read() as db:
            full = units.load_unit(db, generation, account, chat)
            statements = []
            db.set_trace_callback(statements.append)
            summary = units.load_integrity_metadata(db, generation, account, chat)
            db.set_trace_callback(None)
            assert summary.header == full.header
            assert decode_manifest(summary) == decode_manifest(full)
            assert not hasattr(summary, 'node_ids') and not hasattr(summary, 'edge_ids')
            assert not any('u.node_ids' in sql or 'u.edge_ids' in sql for sql in statements)
            assert units.load_integrity_metadata(db, 'wrong', account, chat) is None
            assert units.load_integrity_metadata(db, generation, account_ref('other'), chat) is None
            assert units.load_integrity_metadata(db, generation, account, conversation_ref(ACCOUNT, 'other')) is None
    finally:
        cleanup(f)
