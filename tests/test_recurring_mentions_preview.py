"""Recurring phrase ranking, exact source checks, and one-use preview persistence."""
from datetime import timedelta
import json

import pytest

from app.analytics import nlp_preview as preview
from app.analytics.nlp_preview_execution import PreviewComputeOwner
from app.analytics.nlp_preview_scope import freeze_preview_scope
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.models.text_analysis_preview import NoPreviewOutput, RecurringMentionPreview
from app.models.text_analysis_preview_execution import RecurringMentionSavedEnvelope
from app.nlp.preview_parameters import RECURRENCE_SELECTION_POLICY, RECURRENCE_SOURCE_DIGEST
from app.nlp.profiles import read_profile
from tests.continuous_analytics_fixture import ACCOUNT, advance, insert_message
from tests.test_history_kind_adoption import synthetic_kind
from tests.test_nlp_artifact_store import example
from tests.test_nlp_preview import case, frame, source
from tests.text_analysis_preview_fixture import (
    close_preview_fixture, make_preview_fixture, prepare_preview, preview_http_client,
)

pytestmark = pytest.mark.ci_tier('integration')


def mentions_only(joint):
    document = joint.model_dump(mode='json')
    document.update(schema_version='nlp-profile.v3', revision='mentions')
    document['bindings'] = [b for b in document['bindings'] if b['task'] == 'concept_mentions']
    component = document['components'][0]
    component['tasks'] = [t for t in component['tasks'] if t['task'] == 'concept_mentions']
    component['executed_labels'] = [t for t in component['executed_labels'] if t['task'] == 'concept_mentions']
    return read_profile(json.dumps(document))


def ranked_case(rows):
    messages = [source(i, f'chat-{i}', text).model_copy(update={'sender_id': sender})
                for i, (sender, text) in enumerate(rows)]
    spec, joint, members, messages = case(messages)
    profile = mentions_only(joint)
    spec = spec.model_copy(update={'algorithm_profile_sha256': profile.identity_digest(),
                                   'policy_identity_sha256': RECURRENCE_SOURCE_DIGEST})
    return spec, profile, members, messages


def test_ranking_uses_people_then_messages_then_exact_normalized_phrase():
    data = ranked_case([('a', 'Café reed reed solo'), ('b', 'Cafe\u0301 reed'),
                        ('a', 'Café REED solo'), ('b', 'reed')])
    spans = [
        [('concept.a', 0, 4), ('concept.b', 0, 4), ('concept.a', 5, 9), ('concept.a', 10, 14),
         ('concept.a', 15, 19)],
        [('concept.b', 0, 5), ('concept.b', 6, 10)],
        [('concept.b', 0, 4), ('concept.a', 5, 9), ('concept.a', 10, 14)],
        [('concept.a', 0, 4)],
    ]
    for order in ((0, 1, 2, 3), (3, 1, 2, 0)):
        accumulator = preview.PreviewAccumulator(*data[:3])
        for i in order:
            accumulator.add(i, data[3][i], frame(data, i, mentions=spans[i]))
        result = accumulator.finish()
        assert isinstance(result, RecurringMentionPreview)
        summary, = result.summaries
        assert (summary.literal_key, summary.supporting_senders, summary.supporting_messages,
                summary.occurrences, summary.additional_phrases) == ('Café', 2, 3, 3, 1)
        assert summary.ranking_scope == 'frozen_scope'
        encoded = preview.encode_preview(result)
        assert b'reed' not in encoded and b'solo' not in encoded and b'concept.a' not in encoded
        assert preview.validate_preview_output(*data[:3], encoded) == result
        for member, message in zip(data[2], data[3]):
            preview.validate_preview_source(result, data[1], member, message)
        forged = result.model_copy(update={'summaries': (summary.model_copy(update={'literal_key': 'CAFE'}),)})
        with pytest.raises(ValueError, match='literal_mismatch'):
            preview.validate_preview_source(forged, data[1], data[2][0], data[3][0])


def test_extra_sender_outranks_message_frequency_and_incomplete_scope_hides_additional_phrases():
    data = ranked_case([('a', 'lime pea'), ('b', 'lime pea'), ('c', 'lime pea'),
                        ('a', 'lime pea'), ('a', 'lime pea'), ('d', 'lime pea')])
    accumulator = preview.PreviewAccumulator(*data[:3])
    for i in range(5):
        selected = []
        if i < 3:
            selected.append(('concept.a', 0, 4))
        if i != 2:
            selected.append(('concept.a', 5, 8))
        outcomes = {'concept_mentions': 'partial'} if i == 2 else {}
        accumulator.add(i, data[3][i], frame(data, i, mentions=selected, outcomes=outcomes))
    accumulator.unprocessed(5)
    result = accumulator.finish()
    summary, = result.summaries
    assert (summary.literal_key, summary.supporting_senders, summary.supporting_messages) == ('lime', 3, 3)
    assert (summary.complete_supporting_messages, summary.partial_supporting_messages) == (2, 1)
    assert summary.additional_phrases is None and summary.ranking_scope == 'assessed_portion'
    assert result.tasks[0].partial == result.tasks[0].unprocessed == 1


@pytest.mark.parametrize(('extra_message', 'winner'), [(False, 'Ａ'), (True, '😀')])
def test_distinct_message_count_precedes_unicode_scalar_tiebreak(extra_message, winner):
    rows = [('a', 'Ａ😀'), ('b', 'Ａ😀')]
    if extra_message:
        rows.append(('a', 'Ａ😀'))
    data = ranked_case(rows)
    accumulator = preview.PreviewAccumulator(*data[:3])
    for i, message in enumerate(data[3]):
        mentions = [('concept.a', 1, 2)]
        if i < 2:
            mentions.append(('concept.a', 0, 1))
        accumulator.add(i, message, frame(data, i, mentions=mentions))
    summary, = accumulator.finish().summaries
    assert summary.literal_key == winner
    assert summary.supporting_messages == len(rows)
    assert summary.additional_phrases == 1


def test_one_person_across_conversations_and_unrepresentable_literal_leave_no_result():
    data = ranked_case([('a', 'reed'), ('a', 'reed')])
    accumulator = preview.PreviewAccumulator(*data[:3])
    for i in (0, 1):
        accumulator.add(i, data[3][i], frame(data, i, mentions=[('concept.a', 0, 4)]))
    assert accumulator.finish().disposition == 'empty'
    data = ranked_case([('a', '  '), ('b', '  ')])
    accumulator = preview.PreviewAccumulator(*data[:3])
    for i in (0, 1):
        accumulator.add(i, data[3][i], frame(data, i, mentions=[('concept.a', 0, 2)]))
    outcome = accumulator.finish()
    assert isinstance(outcome, NoPreviewOutput) and outcome.disposition == 'incomplete'


def test_recurring_selection_refuses_joint_profile_and_outbound_source():
    joint_spec, joint, members, _ = case()
    with pytest.raises(ValueError, match='preview_recurring_scope'):
        preview.PreviewAccumulator(joint_spec.model_copy(update={'policy_identity_sha256': RECURRENCE_SOURCE_DIGEST}),
                                   joint, members)
    outbound = source(0).model_copy(update={'direction': 'outbound'})
    spec, joint, members, messages = case((outbound,))
    profile = mentions_only(joint)
    data = (spec.model_copy(update={'policy_identity_sha256': RECURRENCE_SOURCE_DIGEST,
                                    'algorithm_profile_sha256': profile.identity_digest()}),
            profile, members, messages)
    with pytest.raises(ValueError, match='preview_evidence_sender'):
        preview.PreviewAccumulator(*data[:3]).add(0, outbound, frame(data, 0))


@pytest.fixture
def recurring(tmp_path):
    def profile_factory(kind):
        joint, payloads = example(kind)
        return mentions_only(joint), payloads
    x = make_preview_fixture(tmp_path, profile_factory=profile_factory,
                             selection_policy=RECURRENCE_SELECTION_POLICY, findings=True)
    try:
        with x.repository.database.transaction() as db:
            db.execute('UPDATE account_messages SET sent_at=? WHERE creator_account_id=?',
                       ((x.f.clock.now - timedelta(days=2)).isoformat(), ACCOUNT))
            advance(db)
        yield x
    finally:
        close_preview_fixture(x)


def test_exact_latest_100_only_from_current_incoming_counterpart(recurring):
    x = recurring
    now = x.f.clock.now
    with x.repository.database.transaction() as db:
        for i in range(104):
            insert_message(db, 'chat-0', f'eligible-{i:03}', now - timedelta(minutes=104-i), 2*i)
        insert_message(db, 'chat-0', 'wrong-sender', now)
        insert_message(db, 'chat-0', 'outgoing', now, 1)
        insert_message(db, 'chat-0', 'notice', now)
        insert_message(db, 'chat-0', 'future', now + timedelta(seconds=1))
        insert_message(db, 'chat-0', 'expired', now - timedelta(days=90))
        insert_message(db, 'chat-0', 'deleted', now)
        db.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,platform_user_id,
            content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
            VALUES (?,'missing-sender','full',?,'missing',1,1,0,?)""", (ACCOUNT, '\t', now.isoformat()))
        insert_message(db, 'missing-sender', 'blank-id', now)
        db.execute("UPDATE account_messages SET sender_platform_user_id=? WHERE message_id='blank-id'", ('\t',))
        db.execute("UPDATE account_messages SET sender_platform_user_id='different' WHERE message_id='wrong-sender'")
        db.execute("UPDATE account_messages SET event_kind_json=? WHERE message_id='notice'",
                   (json.dumps(synthetic_kind('non_message_event')),))
        db.execute("UPDATE account_messages SET is_deleted=1 WHERE message_id='deleted'")
        advance(db)
    scope, identity, members = freeze_preview_scope(x.repository, x.source, ACCOUNT, now,
        QuestionBudget(QuestionLimits(max_records=100000, wall_clock_ms=30000)),
        selection_policy=RECURRENCE_SELECTION_POLICY)
    assert scope.directions == 'inbound' and scope.source_count == scope.message_cap == identity.source_count == 100
    assert [m.location.message_id for m in members] == [f'eligible-{i:03}' for i in range(4, 104)]
    assert (scope.earliest, scope.latest) == (now - timedelta(minutes=100), now - timedelta(minutes=1))


def test_empty_releases_permission_then_frozen_mentions_run_spends_and_reopens(recurring):
    x = recurring
    compute = PreviewComputeOwner(x.service, x.dependencies, x.source, clock=lambda: x.f.clock.now)
    _, first = prepare_preview(x)
    assert first.request.schema_version == 'text-analysis-preview-prepare.v2'
    binding = x.service.bind_pending(x.service.pending_locators()[0])
    assert compute.run(binding).state == 'failed'
    with x.repository.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'available'
        assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()
    with x.repository.database.transaction() as db:
        db.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,platform_user_id,
            display_name,content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
            VALUES (?,'other-chat','full','other-person',NULL,'other-hash',1,1,0,?)""",
            (ACCOUNT, x.f.clock.now.isoformat()))
        insert_message(db, 'other-chat', 'other-message', x.f.clock.now)
        db.execute("UPDATE account_messages SET sender_platform_user_id='other-person' WHERE message_id='other-message'")
        advance(db)
    body, prepared = prepare_preview(x, confirm=False)
    assert prepared.scope.schema_version == 'text-analysis-preview-scope.v2' and prepared.scope.source_count == 2
    assert [t.task for t in prepared.disclosure.tasks] == ['concept_mentions']
    service = x.configuration.bind(x.source, x.dependencies)
    confirmed = service.confirm(x.http_policy(), body.external_key, prepared.confirmation)
    assert confirmed.scope == prepared.scope
    binding = service.bind_pending(service.pending_locators()[0])
    assert PreviewComputeOwner(service, x.dependencies, x.source, clock=lambda: x.f.clock.now).run(binding).state == 'committed'
    result_id = confirmed.receipt.contribution_id
    x.pair_owner.establish()
    x.authority.revoke(x.reference.reference_id)
    service = x.configuration.bind(x.source, x.dependencies)
    with preview_http_client(x, service=service) as client:
        response = client.get(f'/api/v1/nlp/previews/results/{result_id}/view')
        assert response.status_code == 200, response.text
        saved = RecurringMentionSavedEnvelope.model_validate_json(response.content)
    assert saved.scope == prepared.scope and saved.output.summaries[0].literal_key == 'Thanks'
    assert saved.output.summaries[0].supporting_senders == 2
    for evidence in saved.output.summaries[0].evidence:
        assert service.read_saved_evidence(x.http_policy(), result_id, evidence.reference).text == 'Thanks pricing'
    with x.repository.database.transaction() as db:
        db.execute("UPDATE account_chats SET platform_user_id='replacement' WHERE chat_id='other-chat'")
        advance(db)
    with pytest.raises(ValueError):
        service.read_saved_view(x.http_policy(), result_id)
    with x.repository.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'


def test_first_installed_preview_activates_the_held_permit_and_passes_its_attempt_to_the_worker(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from hashlib import sha256
    from types import SimpleNamespace
    from uuid import uuid4

    from app.analytics.canonical_source import HistoryAnalyticsSource
    from app.analytics.nlp_contribution_sources import ContributionSourceGuard
    from app.core import optional_execution
    from app.models.text_analysis_preview_execution import PreviewPrepareRequest
    from app.nlp.profiles import artifact_closure_digest, canonical_bytes
    from app.persistence.contributions import ContributionRepository
    from app.persistence.history import HistoryRepository
    from app.persistence.nlp_dependencies import DependencyPersistence
    from app.persistence.text_analysis_preview import TextAnalysisPreviewStore
    from app.security.text_analysis_contract import TextAnalysisSchemas, canonical
    from app.nlp import preparation
    from app.services import installed_text_analysis_selection as selections
    from app.services.text_analysis_preview import TextAnalysisPreviewService
    from tests.test_installed_text_analysis import delivery, envelope, registered
    from tests.test_nlp_processing_publication import ConformanceAdapter
    from tests.test_nlp_task_subsets import selected

    profile = selected('mentions')
    profile_id = profile.identity_digest()
    closure = artifact_closure_digest(tuple(a for a in profile.artifacts if a.role in ('weights', 'configuration', 'tokenizer')))
    with registered(tmp_path, TextAnalysisSchemas(), profile_digest=profile_id) as (owner, identity, signers):
        for module in (optional_execution, selections):
            monkeypatch.setattr(module, 'installed_execution_release', lambda: owner.release)
        monkeypatch.setattr(preparation, 'load_preparation_release', lambda: SimpleNamespace(execution=owner.release))
        release_signature, manifest = envelope(owner.release, signers['release'])
        state = canonical(dict(schema='nlp-dependency-authority.v1', epoch=2, trust_sha256='d'*64,
                               catalog_sha256='e'*64, withdrawals=[]))
        with owner.authority.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)', (owner.release.desktop_sha256, release_signature))
            db.execute('UPDATE nlp_dependency_authority SET state_json=?,state_sha256=?', (state.decode(), sha256(state).hexdigest()))
            db.execute("INSERT INTO nlp_dependency_profiles VALUES(?,?,?,'available')", (profile_id, canonical_bytes(profile), closure))
        account = identity.identity.creator_account_id
        database = owner.pair_owner.canonical_database
        now = identity.clock.now.isoformat()
        with database.transaction() as db:
            db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,1,?)', (account, now))
            db.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,platform_user_id,content_hash,
                winning_stream_epoch,winning_source_seq,is_deleted,updated_at) VALUES (?,'chat','full','correspondent','c',1,1,0,?)""", (account, now))
            db.execute("""INSERT INTO account_messages(creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,direction,
                content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
                VALUES (?,'message','chat','correspondent','A garden walk',?,'inbound','m',1,1,0,?)""", (account, now, now))
        source = HistoryAnalyticsSource(HistoryRepository(database))
        repository = ContributionRepository(database, source_guard=ContributionSourceGuard(), clock=lambda: identity.clock.now.timestamp())
        selection = selections.InstalledPreviewSelection(owner, TextAnalysisPreviewStore(repository))
        adapter = ConformanceAdapter(profile)

        @contextmanager
        def retain(chosen, *, binding, attempt):
            assert chosen == profile
            service.check(binding, attempt)
            yield adapter

        dependencies = SimpleNamespace(source=source, persistence=DependencyPersistence(identity.auth),
            gate=owner.authority.database.dependency_authority_gate, preparation_admission=SimpleNamespace(execution=owner.release),
            _admit=lambda current: current == profile, ready=lambda current: current == profile, retain=retain)
        service = TextAnalysisPreviewService(identity.auth, selection, source, dependencies)

        def session():
            return identity.auth.read_bridge_session(identity.session.session_value).policy

        request = PreviewPrepareRequest(schema_version='text-analysis-preview-prepare.v2',
            external_key=str(uuid4()), expected_profile_sha256=profile_id)
        owner.adopt(session(), **delivery('preview', owner, identity, signers, manifest['release_identity']))
        initial_epoch = session().authorization_epoch.value
        prepared = service.prepare(session(), request)
        assert session().authorization_epoch.value == initial_epoch + 1
        assert prepared.scope.source_count == 1 and not adapter.requests
        assert service.prepare(session(), request).receipt == prepared.receipt
        service.confirm(session(), request.external_key, prepared.confirmation)
        binding = service.bind_pending(service.pending_locators()[0])
        result = PreviewComputeOwner(service, dependencies, source, clock=lambda: identity.clock.now).run(binding)
        assert result.state == 'failed' and len(adapter.requests) == 1
        assert owner.preview_state(session()) == 'available'
