"""Authenticated preview requests through the existing computation owner."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from app.analytics import runtime
from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline
from app.analytics.nlp_runtime import FindingsConfiguration
from app.core.config import settings
from app.security.runtime_policy import authorized_account
from app.services.text_analysis_preview import preview_service
from tests.test_text_analysis_enrollment_runtime import eventually
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
    owned = runtime.configure_default_analytics_runtime(x.source, backend='sqlite', projections_path=path,
        canonical_database=x.repository.database, activation=x.f.repositories.projection_activation,
        findings=configuration)
    try:
        await runtime.start_default_analytics_runtime()
        await eventually(lambda: preview_service() is not None)
        service = preview_service()
        assert service.dependencies is owned.findings._service
        yield owned, service
    finally:
        assert await runtime.shutdown_default_analytics_runtime(timeout=15)
        assert preview_service() is None


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
