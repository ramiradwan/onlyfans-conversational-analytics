"""Authenticated preview requests through the existing computation owner."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json

import pytest

from app.analytics import runtime
from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline
from app.analytics.nlp_runtime import FindingsConfiguration
from app.core.config import settings
from app.nlp.profiles import canonical_bytes
from app.security.runtime_policy import authorized_account
from app.services.text_analysis_preview import preview_service
from tests.test_text_analysis_enrollment_runtime import eventually
from tests.continuous_analytics_fixture import ACCOUNT, advance
from tests.test_nlp_artifact_store import example, profile_from
from tests.text_analysis_preview_fixture import make_preview_fixture, close_preview_fixture, preview_http_client, request

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PATH = '/api/v1/nlp/previews'


@asynccontextmanager
async def configured_preview_runtime(x, path):
    assert x.dependencies.close()
    def baseline(account):
        authorized_account(x.identity.owner.rebuild(x.identity.identity), account)
    configuration = FindingsConfiguration(dependencies=x.construct_dependencies, authorize=baseline,
        execution=x.execution_configuration, preview=x.configuration, clock=lambda: x.f.clock.now, interval=.02)
    with runtime._RUNTIME_LOCK:
        previous_configuration = runtime._DEFAULT_CONFIGURATION
        owned = runtime.configure_default_analytics_runtime(x.source, backend='sqlite', projections_path=path,
            canonical_database=x.repository.database, activation=x.f.repositories.projection_activation,
            findings=configuration)
        owned_configuration = runtime._DEFAULT_CONFIGURATION
    try:
        await runtime.start_default_analytics_runtime()
        await eventually(lambda: preview_service() is not None)
        service = preview_service()
        assert service.dependencies is owned.findings._service
        yield owned, service
    finally:
        with runtime._RUNTIME_LOCK:
            assert runtime._DEFAULT_CONFIGURATION is owned_configuration, 'Preview fixture configuration owner changed'
            assert runtime._RUNTIMES.get(id(x.source)) is owned, 'Preview fixture runtime owner changed'
        assert await runtime.shutdown_default_analytics_runtime(timeout=15)
        with runtime._RUNTIME_LOCK:
            assert runtime._DEFAULT_CONFIGURATION is owned_configuration, 'Preview fixture configuration owner changed'
            assert preview_service() is None
            runtime._DEFAULT_CONFIGURATION = previous_configuration


def job_state(x, job):
    with x.repository.database.read() as db:
        return db.execute('SELECT state FROM knowledge_jobs WHERE job_id=?', (job,)).fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['joint', 'split'])
async def test_http_preview_shared_runtime_commits_only_shown_output_and_baseline_reads(tmp_path, monkeypatch, kind):
    x = make_preview_fixture(tmp_path, kind, findings=True)
    monkeypatch.setattr(settings, 'identity_binding_source', 'verified_grants')
    monkeypatch.setattr(settings, 'auth_database_path', x.auth.database.path)
    body = request(x)
    try:
        async with configured_preview_runtime(x, tmp_path/'preview-runtime.sqlite3') as (owned, service):
            assert isinstance(owned.pipeline, LicensedAnalyticsPipeline)
            with preview_http_client(x, service=service) as client:
                response = await asyncio.to_thread(client.post, PATH, json=body.model_dump(mode='json'))
                assert response.status_code == 200, response.text
                prepared = response.json()
                job = prepared['receipt']['job_id']
                contribution = prepared['receipt']['contribution_id']
                assert job_state(x, job) == 'prepared' and not x.analyzed
                response = await asyncio.to_thread(client.post, PATH+'/'+body.external_key+'/confirm',
                    json=prepared['confirmation'])
                assert response.status_code == 200, response.text
                await eventually(lambda: job_state(x, job) == 'committed')
                response = await asyncio.to_thread(client.get, PATH+'/results/'+contribution)
                assert response.status_code == 200, response.text
                shown = response.json()
                assert {row['kind'] for row in shown['summaries']} == {'topic_category', 'mention_category'}
                assert all(row['evidence'] for row in shown['summaries'])
                with x.repository.database.read() as db:
                    saved = bytes(db.execute('SELECT document FROM text_analysis_preview_outputs').fetchone()[0])
                    assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
                    assert not db.execute('SELECT 1 FROM knowledge_members').fetchone()
                    assert not db.execute('SELECT 1 FROM knowledge_contributions').fetchone()
                with service.dependencies.findings.database.read() as db:
                    assert not db.execute('SELECT 1 FROM nlp_records').fetchone()
                calls = len(x.analyzed)
                x.authority.revoke(x.reference.reference_id)
                with x.auth.database.transaction() as db:
                    db.execute('DELETE FROM nlp_dependency_profiles')
                def absent(*args, **kwargs):
                    pytest.fail('Saved preview opened an Engine')
                monkeypatch.setattr(x.environment.engine, 'open', absent)
                response = await asyncio.to_thread(client.get, PATH+'/results/'+contribution)
                assert response.status_code == 200 and response.json() == shown
                retried = await asyncio.to_thread(client.post, PATH, json=body.model_dump(mode='json'))
                assert retried.status_code == 200 and retried.json()['receipt']['job_id'] == job
                assert len(x.analyzed) == calls
                with x.repository.database.read() as db:
                    assert bytes(db.execute('SELECT document FROM text_analysis_preview_outputs').fetchone()[0]) == saved
    finally:
        close_preview_fixture(x)


@pytest.mark.asyncio
async def test_preview_runtime_alternates_on_the_same_executor_without_creating_jobs(tmp_path, monkeypatch):
    x = make_preview_fixture(tmp_path)
    try:
        async with configured_preview_runtime(x, tmp_path/'alternation.sqlite3') as (owned, service):
            # Stop the periodic submitter; invoke its actual turn on the same owned executor.
            owned.findings._stop.set()
            if owned.findings._task is not None:
                await owned.findings._task
            owned.findings._stop.clear()
            turns = []
            monkeypatch.setattr(owned.findings, '_preview_turn', lambda: turns.append('preview') or True)
            monkeypatch.setattr(owned.findings, '_enrollment_turn', lambda: turns.append('enrollment'))
            owned.findings._prefer_preview = True
            for _ in range(4):
                await asyncio.wrap_future(owned.findings._executor.submit(owned.findings._turn))
            assert turns == ['preview', 'enrollment', 'preview', 'enrollment']
            with x.repository.database.read() as db:
                assert not db.execute('SELECT 1 FROM knowledge_jobs').fetchone()
    finally:
        close_preview_fixture(x)


def ranked_preview_profile(kind):
    profile, payloads = example(kind)
    document = profile.model_dump(mode='json')
    for component in document['components']:
        for task in component['tasks']:
            task['enabled_ids'] = (
                ['topic.a', 'topic.b', 'topic.c', 'topic.unused']
                if task['task'] == 'message_topics'
                else ['concept.echo', 'concept.hidden', 'concept.unused']
            )
        component['executed_labels'] = [
            dict(task=task['task'], label_id=category, description='Synthetic ' + category)
            for task in component['tasks'] for category in task['enabled_ids']
        ]
    return profile_from(document), payloads


def no_full_preview_records(x, findings):
    with x.repository.database.read() as db:
        assert not db.execute('SELECT 1 FROM knowledge_members').fetchone()
        assert not db.execute('SELECT 1 FROM knowledge_contributions').fetchone()
    with findings.database.read() as db:
        assert not db.execute('SELECT 1 FROM nlp_records').fetchone()
        assert not db.execute('SELECT 1 FROM nlp_record_origins').fetchone()


def preview_durable_state(x):
    tables = ('knowledge_jobs', 'knowledge_attempts', 'text_analysis_intents',
              'text_analysis_preview_requests', 'text_analysis_preview_admissions',
              'text_analysis_preview_capsules', 'text_analysis_preview_outputs')
    with x.repository.database.read() as db:
        return {table: tuple(tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1'))
                for table in tables}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['joint', 'split'])
async def test_nonwinning_positive_preview_is_shown_only_through_commit_and_original_replay(
    tmp_path, monkeypatch, kind
):
    x = make_preview_fixture(tmp_path, kind, profile_factory=ranked_preview_profile)
    monkeypatch.setattr(settings, 'identity_binding_source', 'verified_grants')
    monkeypatch.setattr(settings, 'auth_database_path', x.auth.database.path)
    original_open = x.environment.engine.open
    emitted = []
    active_findings = []

    @contextmanager
    def positive_candidates(*args, **kwargs):
        with original_open(*args, **kwargs) as session:
            analyze = session.analyze

            def selected(worker_request, cancelled, **bounds):
                value = json.loads(analyze(worker_request, cancelled, **bounds))
                assert len(value['items']) == len(worker_request.items)
                for item, source in zip(value['items'], worker_request.items):
                    item['request_item_id'] = source.request_item_id
                    for task in item['tasks']:
                        payload = task['payload']
                        if payload['task'] == 'message_topics':
                            other = 'topic.b' if source.text.endswith('zero') else 'topic.c'
                            findings = [dict(topic_id='topic.a'), dict(topic_id=other)]
                        else:
                            findings = [
                                dict(concept_id='concept.echo', span=dict(start=2, end=6)),
                                dict(concept_id='concept.echo', span=dict(start=7, end=11)),
                                dict(concept_id='concept.hidden', span=dict(start=12, end=18)),
                            ]
                        payload.update(outcome='assessed_findings', findings=findings)
                        emitted.append((payload['task'], tuple(
                            finding.get('topic_id', finding.get('concept_id')) for finding in findings
                        )))
                no_full_preview_records(x, active_findings[0])
                return json.dumps(value).encode()

            session.analyze = selected
            yield session

    monkeypatch.setattr(x.environment.engine, 'open', positive_candidates)
    path = tmp_path / 'ranked-preview-runtime.sqlite3'
    body = request(x)
    try:
        with x.repository.database.transaction() as db:
            for ordinal, suffix in enumerate(('zero', 'one')):
                text = '😀 echo echo hidden ' + suffix
                db.execute('UPDATE account_messages SET text=?,content_hash=? '
                           'WHERE creator_account_id=? AND message_id=?',
                           (text, hashlib.sha256(text.encode()).hexdigest(), ACCOUNT, f'm-0-{ordinal}'))
            advance(db)
        async with configured_preview_runtime(x, path) as (_, service):
            active_findings.append(service.dependencies.findings)
            committed_frames = []
            original_commit = service.commit

            def observed_commit(binding, token, frame, **bounds):
                no_full_preview_records(x, service.dependencies.findings)
                committed_frames.append(frame)
                return original_commit(binding, token, frame, **bounds)

            monkeypatch.setattr(service, 'commit', observed_commit)
            with preview_http_client(x, service=service) as client:
                prepared = await asyncio.to_thread(client.post, PATH, json=body.model_dump(mode='json'))
                assert prepared.status_code == 200, prepared.text
                preparation = prepared.json()
                job = preparation['receipt']['job_id']
                contribution = preparation['receipt']['contribution_id']
                assert not emitted and job_state(x, job) == 'prepared'
                confirmed = await asyncio.to_thread(client.post, PATH + '/' + body.external_key + '/confirm',
                                                    json=preparation['confirmation'])
                assert confirmed.status_code == 200, confirmed.text
                await eventually(lambda: job_state(x, job) == 'committed')
                shown_response = await asyncio.to_thread(client.get, PATH + '/results/' + contribution)
                assert shown_response.status_code == 200, shown_response.text
                shown = shown_response.json()
                output = service.read_shown(x.http_policy(), contribution)
                frame = canonical_bytes(output)
                assert committed_frames == [frame]
                summaries = {summary.kind: summary for summary in output.summaries}
                topic, mention = summaries['topic_category'], summaries['mention_category']
                assert (topic.label.category_id, topic.supporting_messages, topic.additional_categories) == ('topic.a', 2, 2)
                assert (mention.label.category_id, mention.supporting_messages, mention.occurrences,
                        mention.additional_categories) == ('concept.echo', 2, 4, 1)
                assert all(summary.ranking_scope == 'frozen_scope' for summary in summaries.values())
                assert all(task.assessed == task.eligible == 2 for task in output.tasks)
                assert {entry.ordinal for entry in topic.evidence} == {0, 1}
                assert {(entry.ordinal, entry.reference.span.start, entry.reference.span.end)
                        for entry in mention.evidence} == {(ordinal, start, end)
                            for ordinal in (0, 1) for start, end in ((2, 6), (7, 11))}
                assert {category for task, categories in emitted if task == 'message_topics'
                        for category in categories} == {'topic.a', 'topic.b', 'topic.c'}
                assert {category for task, categories in emitted if task == 'concept_mentions'
                        for category in categories} == {'concept.echo', 'concept.hidden'}
                for entry in mention.evidence:
                    resolved = await asyncio.to_thread(client.post, PATH + '/results/' + contribution + '/evidence',
                                                       json=entry.reference.model_dump(mode='json'))
                    assert resolved.status_code == 200, resolved.text
                    assert resolved.json()['text'][entry.reference.span.start:entry.reference.span.end] == 'echo'
                hidden = mention.evidence[0].reference.model_dump(mode='json')
                hidden['span'] = dict(start=12, end=18)
                refused = await asyncio.to_thread(client.post, PATH + '/results/' + contribution + '/evidence', json=hidden)
                assert refused.status_code == 409
                view = await asyncio.to_thread(client.get, PATH + '/results/' + contribution + '/view')
                assert view.status_code == 200 and view.json()['output'] == shown
                # Declared category inventories remain input disclosure, including unused categories.
                assert {task['task']: task['enabled_ids'] for task in view.json()['disclosure']['tasks']} == {
                    'message_topics': ['topic.a', 'topic.b', 'topic.c', 'topic.unused'],
                    'concept_mentions': ['concept.echo', 'concept.hidden', 'concept.unused'],
                }
                assert all(set(entry) == {'ordinal', 'task', 'assessment', 'reason', 'coverage'}
                           for entry in shown['examination'])
                outcome_response = await asyncio.to_thread(client.get, PATH + '/' + body.external_key)
                assert outcome_response.status_code == 200
                outcome = outcome_response.json()
            no_full_preview_records(x, service.dependencies.findings)
            with x.repository.database.read() as db:
                row = db.execute('SELECT document,document_sha256 FROM text_analysis_preview_outputs').fetchone()
                assert bytes(row[0]) == frame and row[1] == hashlib.sha256(frame).hexdigest()
                assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
            durable = preview_durable_state(x)
        calls = tuple(emitted)

        def no_engine(*args, **kwargs):
            pytest.fail('Saved preview replay opened an Engine')

        monkeypatch.setattr(x.environment.engine, 'open', no_engine)
        async with configured_preview_runtime(x, path) as (_, service):
            with preview_http_client(x, service=service) as client:
                read = await asyncio.to_thread(client.get, PATH + '/results/' + contribution)
                assert read.status_code == 200 and read.json() == shown
                read = await asyncio.to_thread(client.get, PATH + '/results/' + contribution + '/view')
                assert read.status_code == 200 and read.json()['output'] == shown
                retried = await asyncio.to_thread(client.post, PATH, json=body.model_dump(mode='json'))
                assert retried.status_code == 200 and retried.json() == outcome
                retried = await asyncio.to_thread(client.post, PATH + '/' + body.external_key + '/confirm',
                                                  json=preparation['confirmation'])
                assert retried.status_code == 200 and retried.json() == outcome
            assert canonical_bytes(service.read_shown(x.http_policy(), contribution)) == frame
            no_full_preview_records(x, service.dependencies.findings)
            assert preview_durable_state(x) == durable
            assert tuple(emitted) == calls
    finally:
        close_preview_fixture(x)
