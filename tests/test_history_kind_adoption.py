"""Synthetic per-record kind persistence and bounded reply regression cases."""

import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analytics.query_canonical import CanonicalQuestionScope
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.analytics.query_handlers import no_later_creator_reply
from app.analytics.source_snapshot import conversation_digest, read_conversation, scan_identity
from app.persistence.factory import create_canonical_repositories
from tests.continuous_analytics_fixture import ACCOUNT, NOW, cleanup, insert_message, make_fixture
from tests.test_durable_ingestion import ACCOUNT as INGEST_ACCOUNT, commit_seed, delta, identity, payload
from tests.test_reply_source_selection import resolved

pytestmark = [pytest.mark.ci_tier('integration')]


def synthetic_kind(kind='human_message', conversation='chat-0'):
    system = kind == 'non_message_event'
    return dict(schema='connector-history-kind/v1', mapping_version='onlyfans-event-kind/0.2.0',
        evidence_standard='client-parity', client_pin_set_version='onlyfans-client-parity-pins/2026-10-04.1',
        kind=kind, rule_id='CP-S1' if system else 'CP-H1', evidence_state='supported',
        context=dict(account_id='synthetic-platform', conversation_id=conversation,
            generation_id='synthetic-generation', method='GET',
            endpoint='/api2/v2/chats/{id}/messages', surface='native-rest-message-page'),
        source_kind_evidence=dict(availability='available', representations=[dict(root='list', fields=dict(
            responseType=dict(present=True, value_type='string', value_token='literal-message'),
            systemType=dict(present=system, value_type='string' if system else None,
                value_token='literal-call_ended' if system else None)))],
            alias_comparisons=dict(responseType='not-applicable', systemType='not-applicable')))


def add_record(db, record, age, sequence, kind):
    insert_message(db, 'chat-0', record, NOW-age, sequence, text='')
    if kind is not None:
        db.execute('UPDATE account_messages SET event_kind_json=? WHERE creator_account_id=? AND message_id=?',
            (json.dumps(kind), ACCOUNT, record))


def facts(f, question):
    budget = QuestionBudget(QuestionLimits())
    with f.repositories.database.read() as db:
        return list(CanonicalQuestionScope(db, ACCOUNT, budget).conversations(question, budget))


def answer(values, question):
    session = SimpleNamespace(conversations=lambda q, b: values,
        evidence=lambda m, b: (_ for _ in ()).throw(AssertionError('unexpected match')))
    return no_later_creator_reply(session, question, None, QuestionBudget(QuestionLimits()))


def test_system_before_latest_preserves_intervening_creator_reply(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            add_record(db, 'synthetic-request', timedelta(hours=3), 0, synthetic_kind())
            add_record(db, 'synthetic-reply', timedelta(hours=2), 1, synthetic_kind())
            add_record(db, 'synthetic-system', timedelta(hours=1), 2, synthetic_kind('non_message_event'))
        question = resolved(end=NOW-timedelta(hours=2, minutes=30))
        values = facts(f, question)
        assert {m.role for m in values[0].messages} == {'creator', 'participant'}
        page = answer(values, question)
        assert page.total_matching_conversations == 0
        assert page.undetermined_conversation_count == 0
    finally:
        cleanup(f)


@pytest.mark.parametrize('age', [timedelta(hours=1), timedelta(hours=2)])
def test_unknown_latest_or_tied_remains_contender(tmp_path, age):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            add_record(db, 'synthetic-known', timedelta(hours=2), 0, synthetic_kind())
            add_record(db, 'synthetic-unknown', age, 1, None)
        question = resolved(end=NOW-timedelta(hours=1, minutes=30))
        page = answer(facts(f, question), question)
        assert page.total_matching_conversations == 0
        assert page.undetermined_conversation_count == 1
    finally:
        cleanup(f)


def test_unknown_kind_with_system_role_remains_contender(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            add_record(db, 'synthetic-known', timedelta(hours=2), 0, synthetic_kind())
            add_record(db, 'synthetic-unknown', timedelta(hours=1), 1, None)
        question = resolved()
        values = facts(f, question)
        values = [replace(value, messages=tuple(replace(message, role='system')
            if message.kind == 'unknown' else message for message in value.messages)) for value in values]
        page = answer(values, question)
        assert page.total_matching_conversations == 0
        assert page.undetermined_conversation_count == 1
    finally:
        cleanup(f)


@pytest.mark.parametrize('case', ['systems_only', 'earlier_unknown', 'known_tie'])
def test_empty_classification_and_ordering_safeguards(tmp_path, case):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            add_record(db, 'synthetic-first', timedelta(hours=2), 0,
                synthetic_kind('non_message_event') if case == 'systems_only' else synthetic_kind())
            add_record(db, 'synthetic-second', timedelta(hours=3) if case == 'earlier_unknown' else timedelta(hours=2),
                0, None if case == 'earlier_unknown' else synthetic_kind(
                    'non_message_event' if case == 'systems_only' else 'human_message'))
        values = facts(f, resolved())
        if case == 'earlier_unknown':
            from app.analytics.query_handlers import _last
            assert [m.kind for m in _last(values[0].messages)] == ['message']
        else:
            page = answer(values, resolved())
            assert page.total_matching_conversations == 0
            assert page.undetermined_conversation_count == (1 if case == 'known_tie' else 0)
    finally:
        cleanup(f)


def test_question_definition_revision_invalidates_old_answer_definitions(monkeypatch):
    import app.analytics.query_runtime as module

    class Service:
        def __init__(self, reader, registrations, **options):
            self.registration = registrations[0]

        def execute(self, *args, **kwargs):
            return self.registration.revision

    monkeypatch.setattr(module, 'AnalyticsQuestionService', Service)
    resources = module.QuestionResources.__new__(module.QuestionResources)
    resources._remember = lambda policy: 'synthetic-partition'
    resources.source = SimpleNamespace(open_question_scope=lambda *args: None)
    resources.pipeline = SimpleNamespace(projections=None, pipeline_revision='synthetic-revision',
        pipeline_config_digest='synthetic-digest')
    resources.evidence, resources._pending_questions = None, None
    resources.secret, resources.clock = b'synthetic-secret', lambda: NOW
    assert resources._execute(None, resolved().plan.model_dump(), cancellation_check=None) == 'canonical.client-parity.v1'


@pytest.mark.parametrize('question_id', ['no_later_creator_reply.v1', 'pricing_discussions.v1'])
@pytest.mark.parametrize('mutation,expected', [
    ('none', 'message'), ('system', 'system'), ('missing', 'unknown'),
    ('mapping_only', 'unknown'), ('legacy', 'unknown'), ('schema', 'unknown'),
    ('mapping', 'unknown'), ('standard', 'unknown'), ('state', 'unknown'),
    ('kind', 'unknown'), ('binding', 'unknown'), ('kind_object', 'unknown'),
    ('pins_object', 'unknown'), ('method', 'unknown'), ('endpoint', 'unknown'),
    ('surface', 'unknown'), ('generation', 'unknown')])
def test_both_import_adapters_require_versioned_per_record_kind(tmp_path, question_id, mutation, expected):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        kind = synthetic_kind()
        if mutation == 'system':
            kind = synthetic_kind('non_message_event')
        elif mutation == 'missing':
            kind = None
        elif mutation == 'mapping_only':
            kind = dict(mapping_version=kind['mapping_version'], kind='human_message')
        elif mutation == 'legacy':
            kind['schema'] = 'connector-event-kind/v2'
        elif mutation == 'binding':
            kind['context']['conversation_id'] = 'synthetic-foreign'
        elif mutation == 'kind_object':
            kind['kind'] = {}
        elif mutation == 'pins_object':
            kind['client_pin_set_version'] = {}
        elif mutation in {'method', 'endpoint', 'surface', 'generation'}:
            key = 'generation_id' if mutation == 'generation' else mutation
            kind['context'][key] = None if mutation == 'generation' else 'synthetic-unsupported'
        elif mutation in {'schema', 'mapping', 'standard', 'state', 'kind'}:
            key = dict(schema='schema', mapping='mapping_version', standard='evidence_standard',
                state='evidence_state', kind='kind')[mutation]
            kind[key] = 'synthetic-unsupported'
        with f.repositories.database.transaction() as db:
            add_record(db, 'synthetic-media-only', timedelta(hours=1), 0, kind)
        question = resolved()
        question.plan = question.plan.model_copy(update={'question': question_id})
        values = facts(f, question)
        kinds = [m.kind for c in values for m in c.messages]
        assert kinds == ([] if question_id == 'no_later_creator_reply.v1' and expected == 'system' else [expected])
    finally:
        cleanup(f)


def test_kind_delta_is_atomic_replayable_and_invalidates_identity(tmp_path):
    repositories = create_canonical_repositories('sqlite', canonical_path=tmp_path/'canonical.sqlite3')
    key = commit_seed(repositories.history)
    try:
        document = delta(11, uuid4()).model_dump(mode='json')
        document['change']['message']['event_kind'] = synthetic_kind(conversation='chat-1')
        first = payload('ingest.delta', document)
        assert repositories.history.commit_delta(key, first).status == 'accepted'
        with repositories.database.read() as db:
            before = scan_identity(db, INGEST_ACCOUNT, 1)[0]
            stored = read_conversation(db, INGEST_ACCOUNT, 'chat-1')
            assert stored['messages'][0]['event_kind'] == document['change']['message']['event_kind']
            assert conversation_digest(stored) == scan_identity(db, INGEST_ACCOUNT, 1)[1]['chat-1']
        assert repositories.history.commit_delta(key, first).status == 'duplicate'
        document.update(event_id=str(uuid4()), source_seq=12)
        document['change']['message']['event_kind'] = synthetic_kind('non_message_event', 'chat-1')
        assert repositories.history.commit_delta(key, payload('ingest.delta', document)).status == 'accepted'
        with repositories.database.read() as db:
            assert scan_identity(db, INGEST_ACCOUNT, 1)[0] != before
            assert read_conversation(db, INGEST_ACCOUNT, 'chat-1')['messages'][0]['event_kind']['kind'] == 'non_message_event'
        document.update(event_id=str(uuid4()), source_seq=13)
        document['change']['message']['event_kind']['context']['conversation_id'] = 'synthetic-foreign'
        with pytest.raises(ValueError):
            payload('ingest.delta', document)
    finally:
        if repositories.temporary_directory is not None:
            repositories.temporary_directory.cleanup()


def test_kind_snapshot_preserves_metadata_and_allows_metadata_only_replacement(tmp_path):
    repositories = create_canonical_repositories('sqlite', canonical_path=tmp_path/'canonical.sqlite3')
    key = commit_seed(repositories.history)
    try:
        message = delta(11, uuid4()).model_dump(mode='json')['change']['message']
        for index, kind in enumerate(['human_message', 'non_message_event']):
            snapshot = uuid4()
            message['event_kind'] = synthetic_kind(kind, 'chat-1')
            common = identity(snapshot)
            begin = payload('ingest.snapshot', dict(**common, frame_kind='begin', through_seq=20+index,
                chunk_count=1, record_counts=dict(chats=0, messages=1, coverage_evidence=0), max_frame_bytes=524288))
            chunk = payload('ingest.snapshot', dict(**common, frame_kind='chunk', chunk_index=0,
                entity_kind='message', records=[dict(tombstone=False, message=message)]))
            commit = payload('ingest.snapshot', dict(**common, frame_kind='commit', chunk_count=1))
            assert repositories.history.begin_snapshot(key, begin).status == 'accepted'
            assert repositories.history.add_snapshot_chunk(key, chunk).status == 'accepted'
            assert repositories.history.commit_snapshot(key, commit).status == 'accepted'
            with repositories.database.read() as db:
                assert read_conversation(db, INGEST_ACCOUNT, 'chat-1')['messages'][0]['event_kind'] == message['event_kind']
    finally:
        if repositories.temporary_directory is not None:
            repositories.temporary_directory.cleanup()
