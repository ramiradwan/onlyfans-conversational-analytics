"""Enrollment HTTP commands through background execution and saved reads."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from fastapi import FastAPI
import pytest

from app.api.endpoints import insights, text_analysis_execution
from app.analytics.errors import ProjectionUnavailable
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.factory import create_analytics_stores
from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline
from app.analytics.nlp_erasure import FindingsErasure
from app.analytics.nlp_literals import _POLICY_BYTES, checked_publication
from app.analytics.nlp_recovery import RecoveryCoordinator
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.query_runtime import QuestionResources
from app.core.config import settings
from app.models.insights import FindingsRequest
from app.models.knowledge_contributions import record_digest
from app.models.nlp_observations import ObservationLinksRequest
from app.persistence.nlp_dependencies import DependencyPersistence
from app.persistence.retention import CreatorVaultRetention
from app.security.analysis_authorization import clear_analysis_policies, require_cached_analysis_run
from app.security.runtime_policy import RuntimeAuthorizationDenied
from app.services import insights_service
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, insert_message
from tests.test_nlp_recovery import complete
from tests.test_text_analysis_enrollment_runtime import configured_execution_runtime, eventually, published
from tests.text_analysis_execution_fixture import (
    close_execution_fixture, command, execution_http_client, make_execution_fixture,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PATH = '/api/v1/nlp/execution'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['joint', 'split'])
async def test_enrolled_findings_recover_links_and_retire_sources(tmp_path, monkeypatch, kind):
    x = make_execution_fixture(tmp_path, kind)
    monkeypatch.setattr(settings, 'identity_binding_source', 'verified_grants')
    monkeypatch.setattr(settings, 'auth_database_path', x.auth.database.path)
    clear_analysis_policies()
    original_open = x.environment.engine.open
    analyzed = []
    resources = runtime_projections = None
    path = tmp_path/'runtime.sqlite3'

    @contextmanager
    def with_findings(*args, **kwargs):
        with original_open(*args, **kwargs) as session:
            analyze = session.analyze

            def nonempty(request, cancelled, **bounds):
                analyzed.append(request.request_id)
                value = json.loads(analyze(request, cancelled, **bounds))
                component = x.profile.component(request.component_id)
                for item, source in zip(value['items'], request.items):
                    item['request_item_id'] = source.request_item_id
                    for task, descriptor in zip(item['tasks'], component.tasks):
                        finding = ({'topic_id': descriptor.enabled_ids[0]}
                            if descriptor.task == 'message_topics' else
                            {'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}})
                        task['payload'].update(outcome='assessed_findings', findings=[finding])
                return json.dumps(value).encode()

            session.analyze = nonempty
            yield session

    def canonical_rows():
        with x.repository.database.read() as db:
            return {table: tuple(tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1,2'))
                    for table in ('knowledge_jobs', 'knowledge_attempts', 'knowledge_members',
                                  'knowledge_contributions', 'text_analysis_intents')}

    def reopen():
        return create_analytics_stores('sqlite', projections_path=path,
            canonical_database=x.repository.database, activation=x.f.repositories.projection_activation,
            canonical_identity_reader=x.source.read_identity, retention_clock=lambda: x.f.clock.now)

    monkeypatch.setattr(x.environment.engine, 'open', with_findings)
    try:
        async with configured_execution_runtime(x, path) as (owned, service):
            runtime_projections = owned.pipeline.projections
            with execution_http_client(x, service=service) as client:
                response = await asyncio.to_thread(client.post, PATH, json=command(x).model_dump(mode='json'))
                assert response.status_code == 200, response.text
            await eventually(lambda: published(owned, service))
            assert len(analyzed) == 2 * len(x.profile.components)
            service.command(x.http_policy(), command(x, 'pause', current=service.read_current(x.http_policy())))
            assert service.read_current(x.http_policy()).state == 'paused'
            with x.repository.database.read() as db:
                contributions = tuple(tuple(row) for row in db.execute(
                    'SELECT s.message_id,c.contribution_id,m.document FROM knowledge_contributions c '
                    'JOIN knowledge_sources s USING(job_id) '
                    'JOIN knowledge_members m USING(job_id,ordinal) ORDER BY s.message_id'))
            assert [row[0] for row in contributions] == ['m-0-0', 'm-0-1']
            with runtime_projections.database.read() as db:
                old_store = db.execute('SELECT store_id FROM projection_store_identity').fetchone()[0]
            originals = canonical_rows()
        assert not owned.findings.observed_failures
        policy = x.identity.owner.rebuild(x.identity.identity)
        references = tuple((key, record_digest(x.repository.get_committed(key, policy=policy)))
                           for _, key, _ in contributions)
        exports = {key: x.repository.export_document(key, policy=policy) for _, key, _ in contributions}
        records = {hashlib.sha256(bytes(raw)).hexdigest(): json.loads(bytes(raw))
                   for _, _, raw in contributions}
        original_bytes = sorted(bytes(row[2]) for row in contributions)
        before = (len(analyzed), x.environment.engine.prepares, tuple(x.environment.requests))
        runtime_projections.close()
        runtime_projections = None
        x.f.stores.projections.close()
        for suffix in ('', '-wal', '-shm'):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        with x.auth.database.transaction() as db:
            db.execute('DELETE FROM nlp_dependency_profiles')

        def unavailable(*args, **kwargs):
            pytest.fail('Saved recovery opened an Engine')

        monkeypatch.setattr(x.environment.engine, 'open', unavailable)
        monkeypatch.setattr(x.environment.engine, 'present', False)
        x.f.stores = reopen()
        with x.f.stores.database.read() as db:
            assert db.execute('SELECT store_id FROM projection_store_identity').fetchone()[0] != old_store
            assert not db.execute('SELECT 1 FROM nlp_records').fetchone()
        coordinator = RecoveryCoordinator(x.repository, DependencyPersistence(x.auth), x.source,
            x.f.stores.projections, policy=policy, clock=lambda: x.f.clock.now)
        ticket = coordinator.begin(ACCOUNT, x.profile, references, intent=str(uuid4()), normalization=_POLICY_BYTES)
        coordinator.step(ticket, limit=1)
        recovered = coordinator.finish(complete(coordinator, ticket))
        assert coordinator.findings.state(ACCOUNT)['mode'] == 'paused'
        assert recovered['literal_availability'] == 'current'
        with x.f.stores.database.read() as db:
            assert sorted(bytes(row[0]) for row in db.execute('SELECT document FROM nlp_records')) == original_bytes
            assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0] == 2
            assert not db.execute("SELECT 1 FROM nlp_jobs WHERE state='running'").fetchone()
            literal = checked_publication(db, recovered['snapshot'])
            assert literal.envelope.records == literal.envelope.occurrences == 2
        assert canonical_rows() == originals
        assert {key: x.repository.export_document(key, policy=policy) for _, key, _ in contributions} == exports

        projection = recovered['generation'].artifact.projection
        resources = QuestionResources(x.source, AnalyticsPipeline(x.source, projections=x.f.stores.projections,
            clock=lambda: x.f.clock.now), clock=lambda: x.f.clock.now)
        request = FindingsRequest(expected_generation=projection.projection_generation,
            expected_projection_digest=projection.projection_digest,
            start_date=NOW-timedelta(days=9), end_date=x.f.clock.now)
        saved = resources.findings(policy, request)
        assert saved.processing_mode == 'paused'
        assert {task.task for task in saved.tasks} == {'message_topics', 'concept_mentions'}
        assert all(task.eligible_messages == task.complete_messages == 2 and task.pending_messages == 0
                   and task.rows[0].supporting_messages == 2 for task in saved.tasks)
        requests, links = {}, {}
        for task in saved.tasks:
            selected = ObservationLinksRequest(expected_generation=projection.projection_generation,
                expected_projection_digest=projection.projection_digest, start_date=request.start_date,
                end_date=request.end_date,
                focus={'kind': 'category', 'selection': {'task': task.task, 'category_id': task.rows[0].category_id}})
            observed = resources.observation_links(policy, selected)
            links[task.task] = observed
            requests[task.task] = selected.model_copy(update={'expected_view_sha256': observed.view_sha256,
                'expected_normalization_sha256': observed.normalization_sha256})
            assert observed.processing_mode == 'paused' and observed.tasks == saved.tasks
            assert observed.projection_generation == projection.projection_generation
            assert observed.projection_digest == projection.projection_digest
            assert observed.snapshot_sha256 == 'sha256:' + recovered['snapshot'].snapshot_id
            assert observed.support.supporting_conversations == 1
            assert observed.support.supporting_messages == observed.support.occurrences == observed.link_page.total == 2
            for link in observed.link_page.items:
                record = records[link.provenance.extraction_record_sha256]
                stored = next(item for item in record['tasks'] if item['result']['payload']['task'] == task.task)
                assert link.source.model_dump(mode='json') == record['source']
                assert link.provenance.profile_sha256 == stored['profile_sha256']
                assert link.provenance.component_id == stored['result']['component_id']
                assert link.provenance.component_sha256 == stored['result']['component_sha256']
                assert link.provenance.extraction_snapshot_sha256 == recovered['snapshot'].snapshot_id
                resolved = resources.resolve(policy, link.reference)
                if task.task == 'message_topics':
                    assert link.kind == 'message_topic' and link.reference.span is None
                    assert resolved.text == 'Thanks pricing'
                else:
                    assert link.kind == 'literal_occurrence' and link.target.literal_key == 'Thanks'
                    assert (link.reference.span.start, link.reference.span.end) == (0, 6)
                    assert resolved.text[link.reference.span.start:link.reference.span.end] == 'Thanks'
        assert links['concept_mentions'].normalization_sha256 == 'sha256:' + literal.publication_id
        retired_source = json.loads(bytes(contributions[0][2]))['source']['source_ref']
        retired_evidence = next(link.reference for link in links['concept_mentions'].link_page.items
                                if link.source.source_ref == retired_source)
        survivor = contributions[1][1]
        target = CreatorVaultRetention(x.repository.database, clock=lambda: x.f.clock.now).delete_message(ACCOUNT, 'm-0-0')
        with x.f.stores.database.read() as db:
            assert sorted(bytes(row[0]) for row in db.execute('SELECT document FROM nlp_records')) == original_bytes
        for selected in requests.values():
            with pytest.raises(ProjectionUnavailable):
                resources.observation_links(policy, selected)
        with pytest.raises(ProjectionUnavailable):
            resources.findings(policy, request)
        with pytest.raises(EvidenceUnavailable):
            resources.resolve(policy, retired_evidence)
        erasure = FindingsErasure(x.source, x.f.stores.projections, clock=lambda: x.f.clock.now)
        for _ in range(80):
            if erasure.step(ACCOUNT, target, account_wide=0):
                break
        else:
            pytest.fail('Bounded erasure did not finish')
        assert x.repository.export_document(survivor, policy=policy) == exports[survivor]
        resources.close()
        assert resources.wait_closed()
        resources = None
        x.f.stores.projections.close()
        x.f.stores = reopen()
        with x.f.stores.database.read() as db:
            assert [bytes(row[0]) for row in db.execute('SELECT document FROM nlp_records')] == [bytes(contributions[1][2])]
            assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0] == 1
            assert not db.execute('SELECT 1 FROM nlp_literal_publications').fetchone()
            assert not db.execute('SELECT 1 FROM nlp_literal_derivations WHERE record_digest=?',
                                  (hashlib.sha256(bytes(contributions[0][2])).hexdigest(),)).fetchone()
        coordinator = RecoveryCoordinator(x.repository, DependencyPersistence(x.auth), x.source,
            x.f.stores.projections, policy=policy, clock=lambda: x.f.clock.now)
        with pytest.raises(ValueError, match='knowledge_contribution_unavailable'):
            coordinator.begin(ACCOUNT, x.profile, references, intent=str(uuid4()), normalization=_POLICY_BYTES)
        resources = QuestionResources(x.source, AnalyticsPipeline(x.source, projections=x.f.stores.projections,
            clock=lambda: x.f.clock.now), clock=lambda: x.f.clock.now)
        for selected in requests.values():
            with pytest.raises(ProjectionUnavailable):
                resources.observation_links(policy, selected)
        assert x.repository.export_document(survivor, policy=policy) == exports[survivor]
        assert (len(analyzed), x.environment.engine.prepares, tuple(x.environment.requests)) == before
        assert canonical_rows()['knowledge_contributions'] == tuple(
            row for row in originals['knowledge_contributions'] if row[0] == survivor)
        assert canonical_rows()['knowledge_jobs'] == tuple(
            row for row in originals['knowledge_jobs'] if row[0] == exports[survivor]['job']['job_id'])
    finally:
        try:
            if resources is not None:
                resources.close()
                assert resources.wait_closed()
        finally:
            try:
                if runtime_projections is not None:
                    runtime_projections.close()
            finally:
                try:
                    x.f.stores.projections.close()
                finally:
                    clear_analysis_policies()
                    close_execution_fixture(x)


def test_execution_routes_are_registered_with_application():
    from app.main import app

    paths = app.openapi()['paths']
    for expected in text_analysis_execution.router.routes:
        for method in expected.methods:
            assert paths[expected.path][method.lower()]['operationId'] == expected.unique_id


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['joint', 'split'])
async def test_http_enrollment_seals_replays_and_publishes_both_tasks(tmp_path, monkeypatch, kind):
    x = make_execution_fixture(tmp_path, kind)
    monkeypatch.setattr(settings, 'identity_binding_source', 'verified_grants')
    monkeypatch.setattr(settings, 'auth_database_path', x.auth.database.path)
    clear_analysis_policies()
    original_open = x.environment.engine.open
    acquisitions = list(x.environment.requests)
    analyzed = []

    @contextmanager
    def with_findings(*args, **kwargs):
        with original_open(*args, **kwargs) as session:
            analyze = session.analyze

            def nonempty(request, cancelled, **bounds):
                analyzed.append(request.request_id)
                response = json.loads(analyze(request, cancelled, **bounds))
                component = x.profile.component(request.component_id)
                for item in response['items']:
                    for task, descriptor in zip(item['tasks'], component.tasks):
                        finding = ({'topic_id': descriptor.enabled_ids[0]}
                            if descriptor.task == 'message_topics' else
                            {'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}})
                        task['payload'].update(outcome='assessed_findings', findings=[finding])
                return json.dumps(response).encode()

            session.analyze = nonempty
            yield session

    monkeypatch.setattr(x.environment.engine, 'open', with_findings)
    try:
        async with configured_execution_runtime(x, tmp_path/'runtime.sqlite3') as (owned, service):
            assert isinstance(owned.pipeline, LicensedAnalyticsPipeline)
            with x.repository.database.read() as db:
                assert db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] == 0
            assert not analyzed
            reads = x.source.full_reads
            cmd = command(x)
            with execution_http_client(x, service=service) as client:
                response = await asyncio.to_thread(client.post, PATH, json=cmd.model_dump(mode='json'))
                assert response.status_code == 200, response.text
                accepted = response.json()
                recovered = await asyncio.to_thread(client.get, PATH+'/commands/'+cmd.command_key)
                assert recovered.status_code == 200 and recovered.json() == accepted

            await eventually(lambda: published(owned, service))
            with x.repository.database.read() as db:
                children = db.execute('SELECT enrollment_id FROM text_analysis_intents').fetchall()
                originals = sorted(bytes(row[0]) for row in db.execute('SELECT document FROM knowledge_members'))
                assert len(children) == len(originals) == 2
                assert {row[0] for row in children} == {accepted['current']['enrollment_id']}
                assert {row[0] for row in db.execute('SELECT state FROM knowledge_jobs')} == {'committed'}
            with service.dependencies.findings.database.read() as db:
                assert sorted(bytes(row[0]) for row in db.execute('SELECT document FROM nlp_records')) == originals
                assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0] == 2

            assert x.source.full_reads == reads
            projection = owned.pipeline.projections.get(ACCOUNT)
            with pytest.raises(RuntimeAuthorizationDenied):
                require_cached_analysis_run(x.auth, ACCOUNT)
            app = FastAPI()
            app.include_router(insights.router)
            with execution_http_client(x, service=service, app=app) as client:
                current = await asyncio.to_thread(client.get, '/api/v1/insights/projection')
                assert current.status_code == 200, current.text
                assert current.json() == projection.model_dump(mode='json')
                full = await asyncio.to_thread(client.get, '/api/v1/insights/full')
                assert full.status_code == 200, full.text
                for field in ('source_revision', 'projection_generation', 'projection_digest',
                              'canonical_content_digest', 'graph_digest', 'pipeline_revision',
                              'pipeline_config_digest', 'pipeline_identity_digest'):
                    assert full.json()[field] == getattr(projection, field)
                response = await asyncio.to_thread(client.get, '/api/v1/insights/findings', params={
                    'expected_generation': current.json()['projection_generation'],
                    'expected_projection_digest': current.json()['projection_digest'],
                })
                assert response.status_code == 200, response.text
                assert {task['task'] for task in response.json()['tasks']} == {'message_topics', 'concept_mentions'}
            baseline = x.identity.owner.rebuild(x.identity.identity)
            assert baseline.text_analysis_authority is None
            saved_reads = x.source.full_reads
            saved = await asyncio.to_thread(owned.questions.findings, baseline, FindingsRequest(
                expected_generation=projection.projection_generation,
                expected_projection_digest=projection.projection_digest,
                start_date=NOW-timedelta(days=9), end_date=x.f.clock.now))
            assert x.source.full_reads == saved_reads
            assert {task.task for task in saved.tasks} == {'message_topics', 'concept_mentions'}
            assert all(task.rows and task.complete_messages == 2 and task.pending_messages == 0
                       for task in saved.tasks)
            mentions = [item for item in saved.evidence_page.items if item.task == 'concept_mentions']
            assert len(mentions) == 2
            assert all(item.reference.span.start == 0 and item.reference.span.end == 6 for item in mentions)
            assert len(analyzed) == 2 * len(x.profile.components)
            assert x.environment.requests == acquisitions

            service.command(x.http_policy(), command(x, 'pause', current=service.read_current(x.http_policy())))
            async with asyncio.timeout(30):
                while True:
                    retained = await asyncio.to_thread(owned.pipeline.projections.get, ACCOUNT)
                    if retained is not None and retained.projection_generation > projection.projection_generation:
                        try:
                            paused = await asyncio.to_thread(owned.questions.findings, x.http_policy(), FindingsRequest(
                                expected_generation=retained.projection_generation,
                                expected_projection_digest=retained.projection_digest))
                        except ProjectionUnavailable:
                            pass
                        else:
                            if paused.processing_mode == 'paused':
                                break
                    await asyncio.sleep(.02)
            assert await owned.findings.close(15)
            x.authority.revoke(x.reference.reference_id)
            with x.auth.database.transaction() as db:
                db.execute('DELETE FROM nlp_dependency_profiles')
            def unavailable(*args, **kwargs):
                pytest.fail('Saved publication attempted execution or reconstruction')
            monkeypatch.setattr(x.environment.engine, 'open', unavailable)
            monkeypatch.setattr(owned.pipeline, 'build_candidate', unavailable)
            assert await insights_service.active_projection(ACCOUNT) == retained
            assert (await insights_service.get_full_snapshot(ACCOUNT)).analytics.pipeline_revision == retained.pipeline_revision
            assert (await insights_service.fetch_findings(x.http_policy(), FindingsRequest(
                expected_generation=retained.projection_generation,
                expected_projection_digest=retained.projection_digest))).processing_mode == 'paused'
            for field, value in (('pipeline_revision', 'analytics.pipeline.invalid'),
                                 ('pipeline_config_digest', 'sha256:'+'f'*64)):
                with monkeypatch.context() as changed:
                    changed.setattr(owned.pipeline, field, value)
                    with pytest.raises(ProjectionUnavailable):
                        await insights_service.active_projection(ACCOUNT)
            with monkeypatch.context() as changed:
                changed.setattr(owned.pipeline, '_retention_clock', lambda: x.f.clock.now+timedelta(days=91))
                with pytest.raises(ProjectionUnavailable):
                    await insights_service.active_projection(ACCOUNT)
            account = await owned.scheduler.canonical_account(ACCOUNT)
            if kind == 'joint':
                with x.repository.database.transaction() as db:
                    db.execute('DELETE FROM knowledge_jobs WHERE job_id=(SELECT job_id FROM knowledge_contributions LIMIT 1)')
            else:
                with service.dependencies.findings.database.transaction() as db:
                    db.execute('DELETE FROM nlp_generation_refs WHERE generation_id=(SELECT generation_id '
                               "FROM projection_generations WHERE status='active')")
            assert not await asyncio.to_thread(insights_service._saved_projection_is_current,
                retained, owned, ACCOUNT, account, lambda: False)
    finally:
        clear_analysis_policies()
        close_execution_fixture(x)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['joint', 'split'])
async def test_runnable_enrollment_and_fresh_resume_refuse_unavailable_local_authority(
    tmp_path, monkeypatch, kind
):
    x = make_execution_fixture(tmp_path, kind)
    monkeypatch.setattr(settings, 'identity_binding_source', 'verified_grants')
    monkeypatch.setattr(settings, 'auth_database_path', x.auth.database.path)
    clear_analysis_policies()
    original_open = x.environment.engine.open
    opened, analyzed, denied = [], [], []

    @contextmanager
    def recorded_engine(*args, **kwargs):
        opened.append(True)
        with original_open(*args, **kwargs) as session:
            analyze = session.analyze

            def nonempty(worker_request, cancelled, **bounds):
                analyzed.append(worker_request.request_id)
                value = json.loads(analyze(worker_request, cancelled, **bounds))
                component = x.profile.component(worker_request.component_id)
                for item, source in zip(value['items'], worker_request.items):
                    item['request_item_id'] = source.request_item_id
                    for task, descriptor in zip(item['tasks'], component.tasks):
                        finding = (dict(topic_id=descriptor.enabled_ids[0])
                                   if descriptor.task == 'message_topics' else
                                   dict(concept_id=descriptor.enabled_ids[0], span=dict(start=0, end=6)))
                        task['payload'].update(outcome='assessed_findings', findings=[finding])
                return json.dumps(value).encode()

            session.analyze = nonempty
            yield session

    monkeypatch.setattr(x.environment.engine, 'open', recorded_engine)

    def retained_documents():
        with x.repository.database.read() as db:
            return tuple(tuple(row) for row in db.execute(
                'SELECT job_id,ordinal,document FROM knowledge_members ORDER BY job_id,ordinal'))

    async def saved_pending(client, mode):
        async with asyncio.timeout(30):
            while True:
                projection = await asyncio.to_thread(client.get, '/api/v1/insights/projection')
                assert projection.status_code in (200, 503), projection.text
                if projection.status_code == 503:
                    assert projection.json()['detail']['availability'] == 'building'
                if projection.status_code == 200:
                    identity = projection.json()
                    if identity['source_revision'] == x.source.read_identity(ACCOUNT).revision:
                        response = await asyncio.to_thread(client.get, '/api/v1/insights/findings', params={
                            'expected_generation': identity['projection_generation'],
                            'expected_projection_digest': identity['projection_digest'],
                            'start_date': (NOW - timedelta(days=9)).isoformat(),
                            'end_date': x.f.clock.now.isoformat(),
                        })
                        assert response.status_code in (200, 503), response.text
                        if response.status_code == 503:
                            assert response.json()['detail']['availability'] == 'building'
                        if response.status_code == 200:
                            result = response.json()
                            if (result['processing_mode'] == mode and len(result['tasks']) == 2
                                    and all(task['rows'] and task['eligible_messages'] == 3
                                            and task['complete_messages'] == 2 and task['pending_messages'] == 1
                                            for task in result['tasks'])):
                                return result
                await asyncio.sleep(.02)

    path = tmp_path / 'authority-runtime.sqlite3'
    try:
        async with configured_execution_runtime(x, path) as (owned, service):
            with execution_http_client(x, service=service) as client:
                enable = command(x, current=service.read_current(x.http_policy()))
                response = await asyncio.to_thread(client.post, PATH, json=enable.model_dump(mode='json'))
                assert response.status_code == 200, response.text
                enabled = response.json()
                await eventually(lambda: published(owned, service))
                originals = retained_documents()
                assert len(originals) == 2 and analyzed
                # The fresh resume path succeeds while current local authority is available.
                for action in ('pause', 'resume'):
                    body = command(x, action, current=service.read_current(x.http_policy()))
                    response = await asyncio.to_thread(client.post, PATH, json=body.model_dump(mode='json'))
                    assert response.status_code == 200, response.text
                running = service.read_current(x.http_policy())
                assert running.state == 'enabled'
                assert running.enrollment_id == enabled['current']['enrollment_id']
        with x.repository.database.transaction() as db:
            insert_message(db, 'chat-0', 'pending-after-stop', x.f.clock.now - timedelta(seconds=1), 8)
            advance(db)
        # Preserve the signed object and installed profile; the fixture-local reference becomes unavailable.
        x.authority.revoke(x.reference.reference_id)
        assert x.environment.engine.present
        assert [(profile.identity_digest(), state) for profile, _, state in x.environment.persistence.profiles()] == [
            (x.profile.identity_digest(), 'available')
        ]
        original_fence = x.authority.transition_fence

        @contextmanager
        def observed_fence(policy, context, **binding):
            try:
                with original_fence(policy, context, **binding) as held:
                    yield held
            except RuntimeAuthorizationDenied as error:
                denied.append((binding['phase'], str(error)))
                raise

        monkeypatch.setattr(x.authority, 'transition_fence', observed_fence)
        before = (len(opened), len(analyzed), x.environment.engine.prepares, tuple(x.environment.requests))
        async with configured_execution_runtime(x, path) as (owned, service):
            await eventually(lambda: ('prepare', 'text_analysis_reference_unavailable') in denied
                             and owned.findings.failure == 'findings_execution_unavailable')
            assert service.read_current(x.http_policy()) == running
            assert x.environment.engine.current(x.profile.components[0].engine)
            assert service.dependencies.artifacts.reopen(x.profile).profile_sha256 == x.profile.identity_digest()
            app = FastAPI()
            app.include_router(insights.router)
            app.include_router(text_analysis_execution.router)
            with execution_http_client(x, service=service, app=app) as client:
                result = await saved_pending(client, 'enabled')
                assert {task['task'] for task in result['tasks']} == {'message_topics', 'concept_mentions'}
                with service.dependencies.findings.database.read() as db:
                    pending = db.execute("SELECT j.state FROM nlp_jobs j JOIN nlp_sources s USING(account_ref,source_ref) "
                                         'WHERE s.message_id=?', ('pending-after-stop',)).fetchone()
                    assert pending is not None and pending[0] in ('pending', 'running')
                    assert sorted(bytes(row[0]) for row in db.execute('SELECT document FROM nlp_records')) == sorted(
                        bytes(row[2]) for row in originals)
                pause = command(x, 'pause', current=service.read_current(x.http_policy()))
                response = await asyncio.to_thread(client.post, PATH, json=pause.model_dump(mode='json'))
                assert response.status_code == 200, response.text
                paused = service.read_current(x.http_policy())
                assert paused.state == 'paused'
                resume = command(x, 'resume', current=paused)
                response = await asyncio.to_thread(client.post, PATH, json=resume.model_dump(mode='json'))
                assert response.status_code == 409 and response.json()['detail']['code'] == 'text_analysis_refused'
                assert ('start', 'text_analysis_reference_unavailable') in denied
                assert service.read_current(x.http_policy()) == paused
                response = await asyncio.to_thread(client.get, PATH + '/commands/' + resume.command_key)
                assert response.status_code == 404
                replay = await asyncio.to_thread(client.post, PATH, json=enable.model_dump(mode='json'))
                assert replay.status_code == 200 and replay.json()['receipt'] == enabled['receipt']
                assert replay.json()['current'] == paused.model_dump(mode='json')
                await saved_pending(client, 'paused')
            with x.repository.database.read() as db:
                assert db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] == 2
                assert {row[0] for row in db.execute('SELECT state FROM knowledge_jobs')} == {'committed'}
                assert not db.execute('SELECT 1 FROM text_analysis_enrollment_commands WHERE command_key=?',
                                      (resume.command_key,)).fetchone()
            assert retained_documents() == originals
            assert (len(opened), len(analyzed), x.environment.engine.prepares, tuple(x.environment.requests)) == before
            failures = []
            for error in owned.findings.observed_failures:
                frames = []
                trace = error.__traceback__
                while trace is not None:
                    frames.append((trace.tb_frame.f_code.co_name, trace.tb_lineno))
                    trace = trace.tb_next
                failures.append(dict(type=type(error).__name__, frames=frames,
                    code='text_analysis_locator_stale' if type(error) is ValueError
                    and error.args == ('text_analysis_locator_stale',) else 'unexpected_failure'))
            assert not owned.findings.observed_failures, failures
    finally:
        clear_analysis_policies()
        close_execution_fixture(x)
