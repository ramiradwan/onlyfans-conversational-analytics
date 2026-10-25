"""Enrollment HTTP commands through background execution and saved reads."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json

import pytest

from app.api.endpoints import text_analysis_execution
from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline
from app.core.config import settings
from app.models.insights import FindingsRequest
from tests.continuous_analytics_fixture import ACCOUNT, NOW
from tests.test_text_analysis_enrollment_runtime import configured_execution_runtime, eventually, published
from tests.text_analysis_execution_fixture import (
    close_execution_fixture, command, execution_http_client, make_execution_fixture,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PATH = '/api/v1/nlp/execution'


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

            projection = owned.pipeline.projections.get(ACCOUNT)
            baseline = x.identity.owner.rebuild(x.identity.identity)
            assert baseline.text_analysis_authority is None
            saved = await asyncio.to_thread(owned.questions.findings, baseline, FindingsRequest(
                expected_generation=projection.projection_generation,
                expected_projection_digest=projection.projection_digest,
                start_date=NOW-timedelta(days=9), end_date=x.f.clock.now))
            assert {task.task for task in saved.tasks} == {'message_topics', 'concept_mentions'}
            assert all(task.rows and task.complete_messages == 2 and task.pending_messages == 0
                       for task in saved.tasks)
            mentions = [item for item in saved.evidence_page.items if item.task == 'concept_mentions']
            assert len(mentions) == 2
            assert all(item.reference.span.start == 0 and item.reference.span.end == 6 for item in mentions)
            assert len(analyzed) == 2 * len(x.profile.components)
            assert x.environment.requests == acquisitions
            assert x.source.full_reads == reads
    finally:
        close_execution_fixture(x)
