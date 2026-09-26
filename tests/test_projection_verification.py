"""Verify projection streaming against the public model and canonical JSON contract."""

from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock
import weakref

import pytest

from app.analytics import projection_verification as module
from app.analytics.projection_encoding import (
    ENRICHMENT_UNIT_PIPELINE_REVISION, projection_document, projection_digest,
)
from app.analytics.errors import ProjectionBuildCancelled
from app.models.analytics import AnalyticsProjection, MessageEnrichment, ConversationMetrics
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup


@pytest.fixture(scope='module')
def projection(tmp_path_factory):
    fixture = make_fixture(tmp_path_factory.mktemp('projection-verification'), conversations=3, messages=35)
    try:
        yield fixture.pipeline.project_account(ACCOUNT).artifact.projection
    finally:
        cleanup(fixture)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def independent_projection_digest(data):
    if ENRICHMENT_UNIT_PIPELINE_REVISION not in data['pipeline_revision']:
        value = deepcopy(data)
        value.pop('projection_digest', None)
        return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()

    header = {
        key: value for key, value in data.items()
        if key not in {'projection_digest', 'message_enrichments', 'conversation_metrics'}
    }
    digest = hashlib.sha256(b'analytics-projection.enrichment-units.v1\0')
    digest.update(canonical(header).encode())
    metrics = hashlib.sha256()
    for ordinal, item in enumerate(data['conversation_metrics']):
        if ordinal:
            metrics.update(b'\n')
        metrics.update(canonical(item).encode())
    digest.update(b'\0conversation_metrics:')
    digest.update(str(len(data['conversation_metrics'])).encode('ascii'))
    digest.update(b':')
    digest.update(metrics.hexdigest().encode('ascii'))

    order, groups = [], {}
    for item in data['message_enrichments']:
        reference = item['conversation_ref']
        if reference not in groups:
            groups[reference] = []
            order.append(reference)
        groups[reference].append(canonical(item).encode())
    for reference in order:
        content = hashlib.sha256(b'\n'.join(groups[reference])).hexdigest()
        digest.update(b'\0message_enrichment:')
        digest.update(reference.encode('ascii'))
        digest.update(b':')
        digest.update(str(len(groups[reference])).encode('ascii'))
        digest.update(b':')
        digest.update(content.encode('ascii'))
    return 'sha256:' + digest.hexdigest()


@pytest.mark.parametrize('indent', [None, 2])
def test_streamed_projection_matches_independent_canonical_bytes(projection, indent):
    data = projection.model_dump(mode='json')
    text = json.dumps(data, sort_keys=True, indent=indent)
    result = module.verify_projection_document(text)
    assert result.digest == independent_projection_digest(data)
    assert result.digest == projection_digest(projection)
    assert result.streamed
    assert result.message_count == len(projection.message_enrichments)
    assert result.conversation_count == len(projection.conversation_metrics)
    assert result.header.model_dump(mode='json') == {
        name: value for name, value in data.items() if name not in module._ARRAYS}
    assert not hasattr(result.header, 'message_enrichments')
    assert not hasattr(result.header, 'conversation_metrics')


@pytest.mark.parametrize('change', ['reverse_order', 'defaults', 'alias'])
def test_general_documents_keep_public_model_semantics(projection, change):
    data = projection.model_dump(mode='json')
    if change == 'reverse_order':
        data = dict(reversed(sorted(data.items())))
    elif change == 'defaults':
        del data['availability']; del data['schema_version']
    else:
        data['creator_account_id'] = data.pop('account_ref')
    text = json.dumps(data)
    result = module.verify_projection_document(text)
    assert not result.streamed
    assert result.digest == projection_digest(AnalyticsProjection.model_validate_json(text))


@pytest.mark.parametrize('field,value', [
    ('source_revision', -1), ('projection_generation', 0), ('schema_version', '4'),
    ('availability', 'building'), ('account_ref', 'not-an-account'),
    ('pipeline_config_digest', 'not-a-digest'), ('graph_digest', 'sha256:wrong'),
    ('message_enrichments', {}), ('conversation_metrics', None),
])
def test_invalid_top_level_fields_are_rejected(projection, field, value):
    data = projection.model_dump(mode='json'); data[field] = value
    text = canonical(data)
    for validate in (module.verify_projection_document, AnalyticsProjection.model_validate_json):
        with pytest.raises(ValueError):
            validate(text)


@pytest.mark.parametrize('name', ['message_enrichments', 'conversation_metrics'])
@pytest.mark.parametrize('mutation', ['extra', 'missing', 'wrong_type', 'last_item'])
def test_every_array_item_is_validated(projection, name, mutation):
    data = projection.model_dump(mode='json')
    position = -1 if mutation == 'last_item' else 0
    if mutation in ('extra', 'last_item'):
        data[name][position]['unknown'] = True
    elif mutation == 'missing':
        del data[name][0]['account_ref']
    else:
        data[name][0] = 'invalid'
    text = canonical(data)
    with pytest.raises(ValueError):
        module.verify_projection_document(text)


@pytest.mark.parametrize('mutation', ['truncate', 'trailing', 'array_trailing_comma',
    'duplicate_inner', 'root_array', 'nonfinite', 'extra_root'])
def test_invalid_json_is_not_accepted_as_a_verified_projection(projection, mutation):
    text = projection_document(projection)
    if mutation == 'truncate': text = text[:-20]
    elif mutation == 'trailing': text += ' garbage'
    elif mutation == 'array_trailing_comma': text = text.replace('],"creator_metrics"', ',],"creator_metrics"', 1)
    elif mutation == 'duplicate_inner': text = text.replace('"source_ordinal":0', '"source_ordinal":0,"source_ordinal":0', 1)
    elif mutation == 'root_array': text = '[' + text + ']'
    elif mutation == 'nonfinite': text = text.replace('"source_revision":1', '"source_revision":NaN', 1)
    else: text = text[:-1] + ',"z_unknown":0}'
    assert text != projection_document(projection)
    with pytest.raises(ValueError):
        module.verify_projection_document(text)


def test_streamed_records_do_not_retain_full_model_arrays(projection, monkeypatch):
    references = []
    for name, original in list(module._ARRAYS.items()):
        def validate(value, model=original):
            assert sum(ref() is not None for ref in references) <= 1
            item = model.model_validate(value)
            references.append(weakref.ref(item))
            return item
        monkeypatch.setitem(module._ARRAYS, name, type('RecordCheck', (), {'model_validate': staticmethod(validate)}))
    monkeypatch.setattr(AnalyticsProjection, 'model_validate_json', Mock(side_effect=AssertionError('full parse')))
    result = module.verify_projection_document(projection_document(projection))
    assert result.streamed and all(ref() is None for ref in references)


@pytest.mark.parametrize('after', [0, 10, 50, 120])
def test_cancellation_interrupts_record_validation(projection, after):
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls > after:
            raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        module.verify_projection_document(projection_document(projection), check=check)
    assert calls == after + 1


def test_empty_record_arrays_preserve_the_canonical_digest(projection):
    value = projection.model_copy(update={'message_enrichments': [], 'conversation_metrics': []})
    result = module.verify_projection_document(projection_document(value))
    assert result.digest == projection_digest(value)
    assert result.message_count == result.conversation_count == 0


def test_header_validation_tracks_public_field_constraints():
    assert not AnalyticsProjection.__pydantic_decorators__.model_validators
    assert not AnalyticsProjection.__pydantic_decorators__.field_validators
    expected = set(AnalyticsProjection.model_fields) - set(module._ARRAYS)
    assert set(module.ProjectionHeader.model_fields) == expected
    public_schema = AnalyticsProjection.model_json_schema()
    header_schema = module.ProjectionHeader.model_json_schema()
    for name in expected:
        assert header_schema['properties'][name] == public_schema['properties'][name]
    assert set(header_schema['required']) == set(public_schema['required']) - set(module._ARRAYS)


@pytest.mark.parametrize('shared', [False, True])
def test_publication_gates_stream_and_explicit_reads_remain_complete(tmp_path, monkeypatch, shared):
    fixture = make_fixture(tmp_path)
    fixture.stores.projections.reuse_graph_content = shared
    try:
        with monkeypatch.context() as patch:
            forbidden = Mock(side_effect=AssertionError('full projection reconstructed'))
            patch.setattr(AnalyticsProjection, 'model_validate_json', forbidden)
            candidate = fixture.pipeline.build_candidate(ACCOUNT)
            result = fixture.pipeline.publish_candidate(candidate)
            forbidden.assert_not_called()
        artifact = result.artifact
        assert artifact.projection.message_enrichments and artifact.nodes
        assert fixture.stores.projections.get(ACCOUNT) == artifact.projection
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('shared', [False, True])
def test_streamed_metadata_and_full_projection_verify_identical_stored_content(tmp_path, shared):
    from app.analytics.sqlite_projection_store import recompute_generation
    fixture = make_fixture(tmp_path)
    fixture.stores.projections.reuse_graph_content = shared
    try:
        artifact = fixture.pipeline.project_account(ACCOUNT).artifact
        generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
        with fixture.stores.database.read() as db:
            db.execute('BEGIN')
            full = recompute_generation(db, generation)
            bounded = recompute_generation(db, generation, materialize_projection=False)
            assert db.in_transaction
            assert {k: v for k, v in full.items() if k != 'projection'} == {
                k: v for k, v in bounded.items() if k != 'projection'}
            assert full['projection'] == artifact.projection
            assert bounded['projection'].model_dump(mode='json') == artifact.projection.model_dump(
                mode='json', exclude=set(module._ARRAYS))
            with pytest.raises(ValueError, match='projection_materialization_required'):
                recompute_generation(db, generation, materialize_graph=True, materialize_projection=False)
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('stage', ['validated', 'canonical_completed'])
def test_changed_last_record_cannot_pass_activation(tmp_path, stage):
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    fixture = make_fixture(tmp_path)
    try:
        def corrupt(phase, generation):
            if phase == stage:
                with fixture.stores.database.transaction() as db:
                    if ENRICHMENT_UNIT_PIPELINE_REVISION in fixture.pipeline.pipeline_revision:
                        db.execute('DROP TRIGGER conversation_enrichment_units_immutable')
                        db.execute("""UPDATE conversation_enrichment_units
                            SET canonical_digest=?
                            WHERE (creator_account_id,unit_id) IN (
                                SELECT creator_account_id,unit_id
                                FROM conversation_enrichment_refs
                                WHERE generation_id=?
                                ORDER BY ordinal DESC LIMIT 1
                            )""", ('0' * 64, generation))
                    else:
                        db.execute('DROP TRIGGER projection_document_update_blocked')
                        db.execute("""UPDATE analytics_projections SET document_json=json_set(document_json,
                            '$.message_enrichments[#-1].source_ordinal',999999) WHERE generation_id=?""", (generation,))
        fixture.stores.projections.crash_hook = corrupt
        candidate = fixture.pipeline.build_candidate(ACCOUNT)
        with pytest.raises(ProjectionValidationError):
            fixture.pipeline.publish_candidate(candidate)
        assert fixture.stores.projections.get(ACCOUNT) is None
        assert fixture.stores.database.generation(candidate.staged_generation_id).status == 'retired'
    finally:
        cleanup(fixture)


def test_stored_record_cancellation_is_observed_before_graph_verification(tmp_path, monkeypatch):
    from app.analytics import sqlite_projection_store as storage
    from app.analytics import graph_verification
    fixture = make_fixture(tmp_path, conversations=1, messages=100)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
        graph = Mock(side_effect=AssertionError('graph read started'))
        monkeypatch.setattr(graph_verification, 'verify_graph_rows', graph)
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 25:
                raise ProjectionBuildCancelled()
        with fixture.stores.database.read() as db, pytest.raises(ProjectionBuildCancelled):
            storage.recompute_generation(db, generation, materialize_projection=False, check=check)
        graph.assert_not_called()
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('score', [-1, -0.0, 0.125, 1, '0.5'])
@pytest.mark.parametrize('timestamp', ['2026-09-19T12:00:00.000001Z',
    '2026-09-19T15:00:00.120000+03:00', '0001-01-01T00:00:00Z'])
def test_record_normalization_preserves_model_digest(projection, score, timestamp):
    data = projection.model_dump(mode='json')
    item = data['message_enrichments'][0]
    item['sentiment']['score'] = score
    item['sent_at'] = timestamp
    del item['sentiment']['evidence_count']
    data['pipeline_revision'] = 'synthetic-"-\\-Ω-😀'
    text = canonical(data)
    expected = AnalyticsProjection.model_validate_json(text)
    actual = module.verify_projection_document(text)
    assert actual.streamed
    assert actual.digest == projection_digest(expected)


@pytest.mark.parametrize('value', [None, b'{}', 42])
def test_nontext_projection_documents_are_rejected(value):
    with pytest.raises(ValueError, match='projection_document_text_required'):
        module.verify_projection_document(value)


@pytest.mark.parametrize('position', [0, 33, -1])
def test_retention_uses_actual_earliest_message_not_header_dates(projection, position):
    from datetime import datetime, timedelta, timezone
    from app.analytics.retention_store import RetentionBoundSQLiteAnalyticsProjectionStore as Store
    data = projection.model_dump(mode='json')
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data['message_enrichments'][position]['sent_at'] = first.isoformat()
    data['creator_metrics']['active_from'] = '2027-01-01T00:00:00Z'
    value = module.verify_projection_document(canonical(data))
    full = AnalyticsProjection.model_validate(data)
    assert value.first_source_at == first
    for delta in (-1, 0, 1):
        now = first + timedelta(days=90, microseconds=delta)
        actual = Store._source_retention_state(value.header.pipeline_revision, value.first_source_at, now=now)
        assert actual == Store._artifact_retention_state(full, now=now)
        assert actual[0] == (delta >= 0)


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('pipeline', ['bounded', 'clear', 'other'])
def test_retention_summary_matches_full_projection_rules(projection, empty, pipeline):
    from datetime import datetime, timezone
    from app.analytics.retention_store import RetentionBoundSQLiteAnalyticsProjectionStore as Store
    from app.analytics.projection_store import CLEAR_PIPELINE_REVISION
    data = projection.model_dump(mode='json')
    if empty:
        data['message_enrichments'] = []
    if pipeline != 'bounded':
        data['pipeline_revision'] = CLEAR_PIPELINE_REVISION if pipeline == 'clear' else 'unknown.pipeline'
    verified = module.verify_projection_document(canonical(data))
    full = AnalyticsProjection.model_validate(data)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert Store._source_retention_state(verified.header.pipeline_revision,
        verified.first_source_at, now=now) == Store._artifact_retention_state(full, now=now)


def test_retirement_scan_does_not_reconstruct_projection_arrays(tmp_path, monkeypatch):
    from datetime import timedelta
    from tests.continuous_analytics_fixture import advance
    fixture = make_fixture(tmp_path)
    try:
        first = fixture.pipeline.project_account(ACCOUNT).artifact.projection
        with fixture.repositories.database.transaction() as db:
            advance(db)
        fixture.pipeline.publish_candidate(fixture.pipeline.build_candidate(ACCOUNT))
        due = min(m.sent_at for m in first.message_enrichments) + timedelta(days=90)
        with monkeypatch.context() as patch:
            patch.setattr(AnalyticsProjection, 'model_validate_json',
                          Mock(side_effect=AssertionError('full retirement parse')))
            fixture.stores.projections._purge_expired_retired_generations()
            fixture.clock.now = due
            fixture.stores.projections._purge_expired_retired_generations()
        with fixture.stores.database.read() as db:
            assert not db.execute("SELECT 1 FROM projection_generations WHERE status='retired'").fetchone()
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('ordered', [False, True])
def test_retention_reader_validates_all_records(projection, ordered):
    data = projection.model_dump(mode='json')
    if not ordered:
        data = dict(reversed(sorted(data.items())))
    text = json.dumps(data, sort_keys=ordered)
    expected = min(item.sent_at for item in projection.message_enrichments)
    assert module.read_projection_source_time(text) == (projection.pipeline_revision, expected)
    data['message_enrichments'][-1]['sentiment']['score'] = 10
    with pytest.raises(ValueError):
        module.read_projection_source_time(json.dumps(data, sort_keys=ordered))


def test_retention_reader_observes_cancellation(projection):
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 35:
            raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        module.read_projection_source_time(projection_document(projection), check=check)


@pytest.mark.parametrize('field', ['account_ref', 'pipeline_config_digest', 'analyzers', 'graph'])
def test_validation_errors_do_not_echo_rejected_field_contents(projection, field):
    marker = 'SYNTHETIC-REJECTED-FIELD-873'
    data = projection.model_dump(mode='json'); data[field] = marker
    with pytest.raises(ValueError) as error:
        module.verify_projection_document(canonical(data))
    assert marker not in str(error.value)
