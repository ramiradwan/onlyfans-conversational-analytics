"""Reuse a graph read only under the exact verified storage state."""

from dataclasses import replace
from collections import Counter

import pytest

from app.analytics import conversation_graph_sql
from app.analytics.opaque_refs import account_ref
from tests.test_conversation_graph_references import fixture
from tests.continuous_analytics_fixture import ACCOUNT, cold_equal


def observe_reads(fixture, monkeypatch, change=None):
    calls = Counter()
    reading = conversation_graph_sql._read_graph_records
    stage = fixture.stores.projections.stage_built_artifact
    phase = ['build']
    def observed(*args):
        calls[phase[0]] += 1
        return reading(*args)
    def staged(artifact, **kwargs):
        phase[0] = 'stage'
        try:
            if change is not None:
                change(kwargs)
            return stage(artifact, **kwargs)
        finally:
            phase[0] = 'build'
    monkeypatch.setattr(conversation_graph_sql, '_read_graph_records', observed)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', staged)
    return calls


def test_verified_page_read_avoids_second_page_decode(fixture, monkeypatch):
    from app.analytics import conversation_pages

    fixture.stores.projections.reuse_conversation_enrichment_units = False
    fixture.pipeline.project_account(ACCOUNT)
    calls = Counter()
    unpack = conversation_pages.unpack_page
    stage = fixture.stores.projections.stage_built_artifact
    phase = ['build']
    def observed(data):
        calls[phase[0]] += 1
        return unpack(data)
    def staged(artifact, **kwargs):
        phase[0] = 'stage'
        try:
            return stage(artifact, **kwargs)
        finally:
            phase[0] = 'build'
    monkeypatch.setattr(conversation_pages, 'unpack_page', observed)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', staged)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert calls['build'] > 0 and calls['stage'] == 0
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)


def test_storage_change_during_page_decode_forces_staging_decode(fixture, monkeypatch):
    from app.analytics import conversation_pages

    fixture.stores.projections.reuse_conversation_enrichment_units = False
    fixture.pipeline.project_account(ACCOUNT)
    calls, changed = Counter(), []
    unpack = conversation_pages.unpack_page
    stage = fixture.stores.projections.stage_built_artifact
    phase = ['build']
    def observed(data):
        calls[phase[0]] += 1
        result = unpack(data)
        if phase[0] == 'build' and not changed:
            with fixture.stores.database.transaction() as db:
                db.execute('UPDATE generation_content_epoch SET value=value+1')
            changed.append(True)
        return result
    def staged(artifact, **kwargs):
        phase[0] = 'stage'
        try:
            return stage(artifact, **kwargs)
        finally:
            phase[0] = 'build'
    monkeypatch.setattr(conversation_pages, 'unpack_page', observed)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', staged)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert changed and calls['build'] > 0 and calls['stage'] > 0
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)


def test_verified_graph_unit_avoids_predecessor_row_reads(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    calls = observe_reads(fixture, monkeypatch)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert calls['build'] == 0 and calls['stage'] == 0
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)


def test_verified_graph_read_avoids_second_source_scan(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections._conversation_graph_proofs.clear()
    receipts = []
    def remember(kwargs):
        receipts.extend(item.graph_receipt for item in kwargs['conversation_pages'])
    calls = observe_reads(fixture, monkeypatch, remember)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert receipts and all(receipts)
    assert calls['build'] > 0 and calls['stage'] == 0
    result = fixture.pipeline.publish_candidate(candidate)
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('fault', ['disabled', 'missing', 'proof', 'stamp', 'generation',
    'account', 'header', 'changed_storage', 'tracking_removed', 'schema_changed'])
def test_unusable_graph_receipt_rereads_actual_rows(fixture, monkeypatch, fault):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections._conversation_graph_proofs.clear()
    def alter(kwargs):
        items = list(kwargs['conversation_pages'])
        item = items[0]
        receipt = item.graph_receipt
        assert receipt is not None
        if fault == 'disabled':
            fixture.stores.projections.reuse_graph_page_receipts = False
        elif fault in ('changed_storage', 'tracking_removed', 'schema_changed'):
            with fixture.stores.database.transaction() as db:
                if fault == 'tracking_removed':
                    db.execute('DROP TRIGGER generation_content_graph_node_content_update')
                elif fault == 'schema_changed':
                    db.execute('CREATE TABLE synthetic_graph_receipt_change(value INTEGER)')
                else:
                    db.execute('UPDATE generation_content_epoch SET value=value+1')
        else:
            if fault == 'missing':
                receipt = None
            elif fault == 'proof':
                receipt = replace(receipt, proof='0' * 64)
            elif fault == 'stamp':
                receipt = replace(receipt, stamp=('other-store', *receipt.stamp[1:]))
            elif fault == 'generation':
                receipt = replace(receipt, generation_id='other-generation')
            else:
                update = {'account_ref': account_ref('another-account')} if fault == 'account' else {
                    'graph_digest': 'sha256:' + 'f' * 64}
                receipt = replace(receipt, header=receipt.header.model_copy(update=update))
            items[0] = replace(item, graph_receipt=receipt)
            kwargs['conversation_pages'] = tuple(items)
    calls = observe_reads(fixture, monkeypatch, alter)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert calls['build'] > 0 and calls['stage'] > 0
    result = fixture.pipeline.publish_candidate(candidate)
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('every_read', [False, True])
def test_change_during_read_prevents_receipt(fixture, monkeypatch, every_read):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections._conversation_graph_proofs.clear()
    reading = conversation_graph_sql._read_graph_records
    changed = []
    staging = [False]
    def interleaved(*args):
        result = reading(*args)
        if not staging[0] and (every_read or not changed):
            changed.append(True)
            with fixture.stores.database.transaction() as db:
                db.execute('UPDATE generation_content_epoch SET value=value+1')
        return result
    monkeypatch.setattr(conversation_graph_sql, '_read_graph_records', interleaved)
    receipts = []
    def remember(kwargs):
        staging[0] = True
        receipts.extend(item.graph_receipt for item in kwargs['conversation_pages'])
    calls = observe_reads(fixture, monkeypatch, remember)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert len(receipts) == 2
    if every_read:
        assert all(item is None for item in receipts)
    else:
        assert receipts[0] is None
        assert receipts[1] is not None
    assert calls['build'] > 0 and calls['stage'] > 0
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)


def test_graph_receipt_does_not_replace_full_stored_validation(fixture, monkeypatch):
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    fixture.pipeline.project_account(ACCOUNT)
    calls = observe_reads(fixture, monkeypatch)
    changed = []
    def corrupt_after_writes(phase, generation):
        if phase != 'built':
            return
        changed.append(generation)
        with fixture.stores.database.transaction() as db:
            db.execute('DROP TRIGGER graph_node_content_immutable')
            db.execute("UPDATE graph_node_content SET properties_json=json_set(properties_json,'$.character_count',999) WHERE kind='message'")
    fixture.stores.projections.crash_hook = corrupt_after_writes
    with pytest.raises(ProjectionValidationError, match='graph'):
        fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert changed and calls['stage'] == 0
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT status FROM projection_generations WHERE generation_id=?',
                          (changed[0],)).fetchone()[0] == 'building'
    fixture.stores.projections.discard_generation(changed[0])


def test_internal_verified_page_reference_cannot_be_injected(fixture):
    from app.analytics.conversation_page_sql import load_page_header, resolve_page_sets
    from app.analytics.conversation_pages import VerifiedPageReference
    from app.analytics.validation_receipt import content_stamp

    fixture.pipeline.project_account(ACCOUNT)
    store = fixture.stores.projections
    with store.database.transaction() as db:
        row = db.execute('''SELECT s.* FROM conversation_page_sets s
            JOIN projection_generations g USING(generation_id,creator_account_id)
            WHERE g.status='active' LIMIT 1''').fetchone()
        header = load_page_header(
            db, row['generation_id'], row['creator_account_id'],
            row['conversation_ref'], row['input_digest'], row['config_digest'],
        )
        assert header is not None
        marker = VerifiedPageReference(row['generation_id'], header)
        with pytest.raises(ValueError, match='verified_reference_unavailable'):
            list(resolve_page_sets(
                db, store, ACCOUNT, (marker,), check=lambda: None,
                source_stamp=content_stamp(db),
            ))


def test_graph_receipt_requires_a_write_transaction(fixture):
    from app.analytics.conversation_page_sql import resolve_page_sets
    from app.analytics.validation_receipt import content_stamp
    with fixture.stores.database.read() as db:
        stamp = content_stamp(db)
        assert stamp is not None and not db.in_transaction
        with pytest.raises(ValueError, match='conversation_graph_receipt_requires_transaction'):
            list(resolve_page_sets(db, fixture.stores.projections, ACCOUNT, (),
                                   check=lambda: None, source_stamp=stamp))


def test_malformed_receipt_falls_back_without_trusting_its_attributes(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections._conversation_graph_proofs.clear()
    def alter(kwargs):
        kwargs['conversation_pages'] = tuple(replace(item, graph_receipt='not-a-receipt')
                                             for item in kwargs['conversation_pages'])
    calls = observe_reads(fixture, monkeypatch, alter)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert calls['stage'] > 0
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)
