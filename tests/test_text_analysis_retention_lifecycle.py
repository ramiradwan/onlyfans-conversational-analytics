"""Frozen preview retirement across authenticated execution and matched restore."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from fastapi import FastAPI
import pytest

from app.api.endpoints import text_analysis_client, text_analysis_execution, text_analysis_preview
from app.analytics import runtime
from app.models.text_analysis_client import ClientIntentPut, PreviewConfirmIntent
from app.nlp.profiles import canonical_bytes
from app.persistence.authoritative_backup import (
    OfflineAuthoritativeRestoreOwner,
    create_authoritative_backup,
    restore_authoritative_backup,
    verify_authoritative_backup,
)
from app.persistence.retention import CreatorVaultRetention, WORKING_RAW_MAX_DAYS
from app.persistence.deletion_operations import ManagedDeletionOperations
from app.services.derived_deletion import DerivedDeletionService
from app.services.retention_maintenance import RetentionMaintenance
from app.services import retention_maintenance
from app.transport.manager import InMemoryTransportManager
from app.services.text_analysis_client import (
    FixtureTextAnalysisClientConfiguration,
    client_service,
    configure_client_service,
)
from app.services.text_analysis_execution import execution_service
from tests.continuous_analytics_fixture import ACCOUNT, advance
from tests.test_text_analysis_enrollment_runtime import verified_grants
from tests.test_text_analysis_preview_runtime import configured_preview_runtime, eventually, job_state
from tests.text_analysis_execution_fixture import command, execution_http_client
from tests.text_analysis_preview_fixture import make_preview_fixture, close_preview_fixture, request


pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PREVIEWS = '/api/v1/nlp/previews'
EXECUTION = '/api/v1/nlp/execution'
INTENTS = '/api/v1/nlp/client-intents'
HIDDEN = 'm-0-1'


def install_selected_adapter(x):
    """Return both typed tasks, with findings only for the shown source."""
    original = x.environment.engine.open
    x.analyzed = []

    @contextmanager
    def opened(*args, **kwargs):
        with original(*args, **kwargs) as session:
            analyze = session.analyze

            def selected(worker_request, cancelled, **bounds):
                component = x.profile.component(worker_request.component_id)
                value = None
                items = []
                for source in worker_request.items:
                    x.analyzed.append((worker_request.component_id, source.text_sha256))
                    single = worker_request.model_copy(update={'items': (source,)})
                    value = json.loads(analyze(single, cancelled, **bounds))
                    item = value['items'][0]
                    item['request_item_id'] = source.request_item_id
                    if source.text == 'Thanks pricing':
                        for task, descriptor in zip(item['tasks'], component.tasks):
                            finding = (
                                {'topic_id': descriptor.enabled_ids[0]}
                                if descriptor.task == 'message_topics'
                                else {'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}}
                            )
                            task['payload'].update(outcome='assessed_findings', findings=[finding])
                    items.append(item)
                assert value is not None
                value['items'] = items
                return json.dumps(value).encode()

            session.analyze = selected
            yield session

    x.environment.engine.open = opened


@contextmanager
def lifecycle_http(x, service):
    app = FastAPI()
    for router in (text_analysis_client.router, text_analysis_execution.router, text_analysis_preview.router):
        app.include_router(router)
    licensed = execution_service()
    assert licensed is not None and licensed.dependencies is service.dependencies
    journal = FixtureTextAnalysisClientConfiguration(x.auth, x.repository, x.pair_owner).bind(
        x.source, service.dependencies
    )
    assert client_service() is None
    configure_client_service(journal)
    try:
        with execution_http_client(x, service=licensed, app=app) as client:
            yield client
    finally:
        assert client_service() is journal
        configure_client_service(None)


async def post(client, path, body):
    response = await asyncio.to_thread(client.post, path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def retain(client, kind, key, body):
    proposed = ClientIntentPut(kind=kind, key=key, body=body)
    acknowledged = await post(client, INTENTS, proposed.model_dump(mode='json'))
    assert acknowledged['body'] == body.model_dump(mode='json')
    assert acknowledged['body_sha256'] == hashlib.sha256(canonical_bytes(body)).hexdigest()
    return acknowledged


def rows(database, table):
    with database.read() as db:
        return tuple(tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1,2'))


def contribution_bytes(x):
    with x.repository.database.read() as db:
        return tuple(tuple(row) for row in db.execute(
            'SELECT job_id,ordinal,document FROM knowledge_members ORDER BY job_id,ordinal'
        ))


def offline(x):
    return OfflineAuthoritativeRestoreOwner.from_existing(
        root_lease=x.pair_owner.root_lease,
        auth_path=x.auth.database.path,
        canonical_path=x.repository.database.path,
        installation_id=x.pair_owner.installation_id,
    )


@asynccontextmanager
async def deletion_owner(x):
    assert retention_maintenance._DEFAULT_TASK is None
    assert not retention_maintenance._DEFAULT_INFLIGHT
    manager = InMemoryTransportManager(x.f.repositories)
    operations = ManagedDeletionOperations(x.repository.database)
    deletions = DerivedDeletionService(
        operations,
        project_committed=manager.project_committed_state,
        reconcile_findings=runtime.reconcile_findings_erasure,
    )
    try:
        yield operations, deletions
    finally:
        try:
            assert await retention_maintenance.shutdown_default_retention_maintenance(timeout=15)
        finally:
            await manager.stop()


def assert_preview_erased(x, job):
    with x.repository.database.read() as db:
        for table in ('knowledge_jobs', 'knowledge_sources', 'knowledge_attempts', 'knowledge_members',
                      'knowledge_contributions', 'text_analysis_preview_capsules', 'text_analysis_preview_outputs'):
            assert not db.execute(f'SELECT 1 FROM {table} WHERE job_id=?', (job,)).fetchone(), table
        assert db.execute('SELECT state FROM text_analysis_intents WHERE job_id=?', (job,)).fetchone()[0] == 'result_deleted'
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
        assert db.execute('SELECT count(*) FROM text_analysis_preview_requests').fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['joint', 'split'])
@pytest.mark.parametrize('retirement', ['message', 'account', 'expiry'])
async def test_frozen_scope_retirement_survives_restore_and_original_intent_replay(
    tmp_path, monkeypatch, kind, retirement
):
    x = make_preview_fixture(tmp_path, kind)
    verified_grants(monkeypatch, x)
    install_selected_adapter(x)
    path = tmp_path / 'runtime.sqlite3'
    try:
        with x.repository.database.transaction() as db:
            db.execute('UPDATE account_messages SET sent_at=? WHERE message_id=?',
                       ((x.f.clock.now - timedelta(minutes=1)).isoformat(), 'm-0-0'))
            db.execute('UPDATE account_messages SET text=?,content_hash=?,sent_at=? WHERE message_id=?',
                       ('Hidden source', 'hidden-source',
                        (x.f.clock.now - timedelta(days=WORKING_RAW_MAX_DAYS)
                         + timedelta(minutes=5)).isoformat(), HIDDEN))
            db.execute('UPDATE archive_membership SET source_event_at=(SELECT sent_at FROM account_messages m '
                       'WHERE m.creator_account_id=archive_membership.creator_account_id '
                       'AND m.message_id=archive_membership.message_id) WHERE creator_account_id=?', (ACCOUNT,))
            advance(db)
        for database in (x.auth.database, x.repository.database):
            with database.path.open('rb') as stream:
                assert stream.read(16) != b'SQLite format 3' + bytes([0])
        body = request(x)
        async with configured_preview_runtime(x, path) as (owned, service):
            with lifecycle_http(x, service) as client:
                await retain(client, 'preview_prepare', body.external_key, body)
                assert not rows(x.repository.database, 'knowledge_jobs')
                prepared = await post(client, PREVIEWS, body.model_dump(mode='json'))
                job, contribution = (prepared['receipt'][name] for name in ('job_id', 'contribution_id'))
                assert job_state(x, job) == 'prepared' and not x.analyzed
                from app.models.text_analysis_jobs import PreviewConfirmation

                confirmation = PreviewConfirmation.model_validate(prepared['confirmation'])
                confirm_intent = PreviewConfirmIntent(external_key=body.external_key, confirmation=confirmation)
                await retain(client, 'preview_confirm', body.external_key, confirm_intent)
                await post(client, PREVIEWS + '/' + body.external_key + '/confirm', prepared['confirmation'])
                await eventually(lambda: job_state(x, job) == 'committed')
                saved = service.read_shown(x.http_policy(), contribution)
                frame = canonical_bytes(saved)
                with x.repository.database.read() as db:
                    stored = db.execute('SELECT document,document_sha256 FROM text_analysis_preview_outputs '
                                        'WHERE contribution_id=?', (contribution,)).fetchone()
                    assert bytes(stored[0]) == frame
                    assert stored[1] == hashlib.sha256(frame).hexdigest()
                assert saved.source_count == 2
                assert {summary.task for summary in saved.summaries} == {'message_topics', 'concept_mentions'}
                assert len(saved.examination) == 4
                shown = {evidence.ordinal for summary in saved.summaries for evidence in summary.evidence}
                assert len(shown) == 1 and shown < {0, 1}
                for summary in saved.summaries:
                    for evidence in summary.evidence:
                        resolved = service.read_saved_evidence(x.http_policy(), contribution, evidence.reference)
                        assert resolved.text == 'Thanks pricing' and resolved.location.message_id == 'm-0-0'
                        assert evidence.source.source_version == evidence.reference.source_version_digest.removeprefix('sha256:')
                assert saved.profile_sha256 == x.profile.identity_digest()
                assert saved.job_id == job and saved.contribution_id == contribution
                assert b'Hidden source' not in frame
                assert not rows(x.repository.database, 'knowledge_contributions')
                with service.dependencies.findings.database.read() as db:
                    assert not db.execute('SELECT 1 FROM nlp_records').fetchone()
                    assert not db.execute('SELECT 1 FROM nlp_record_origins').fetchone()

                enable = command(x, current=execution_service().read_current(x.http_policy()))
                await retain(client, 'enrollment_command', enable.command_key, enable)
                enabled = await post(client, EXECUTION, enable.model_dump(mode='json'))
                await eventually(lambda: len(rows(x.repository.database, 'knowledge_contributions')) == 2)
                pause = command(x, 'pause', current=execution_service().read_current(x.http_policy()))
                await retain(client, 'enrollment_command', pause.command_key, pause)
                paused = await post(client, EXECUTION, pause.model_dump(mode='json'))
                assert paused['current']['state'] == 'paused'
                assert paused['current']['enrollment_id'] == enabled['current']['enrollment_id']
                original_members = contribution_bytes(x)
                assert len(original_members) == 2
                assert rows(x.repository.database, 'text_analysis_preview_outputs')[0]
                journal = rows(x.repository.database, 'text_analysis_client_intents')
                commands = rows(x.repository.database, 'text_analysis_enrollment_commands')
                requests = rows(x.repository.database, 'text_analysis_preview_requests')
                admissions = rows(x.repository.database, 'text_analysis_preview_admissions')

        calls = tuple(x.analyzed)
        reference_ids = {x.reference.reference_id}
        reference_ids.update(selection.context.reference_id for selection in x.execution_configuration.selection.selections)
        for reference_id in reference_ids:
            x.authority.revoke(reference_id)
        x.environment.engine.present = False
        with x.auth.database.transaction() as db:
            db.execute('DELETE FROM nlp_dependency_profiles')

        def no_engine(*args, **kwargs):
            pytest.fail('Retained recovery opened an Engine')

        monkeypatch.setattr(x.environment.engine, 'open', no_engine)
        backup = tmp_path / 'matched-backup'
        manifest = create_authoritative_backup(x.pair_owner, backup)
        assert verify_authoritative_backup(backup) == manifest
        before = x.pair_owner.establish()
        restored = restore_authoritative_backup(offline(x), backup, now=x.f.clock.now)
        assert restored.incarnation != before.incarnation
        assert contribution_bytes(x) == original_members

        async with configured_preview_runtime(x, path) as (_, service):
            assert canonical_bytes(service.read_shown(x.http_policy(), contribution)) == frame
            assert canonical_bytes(service.read_saved_view(x.http_policy(), contribution).output) == frame
            for summary in saved.summaries:
                for evidence in summary.evidence:
                    assert service.read_saved_evidence(x.http_policy(), contribution, evidence.reference).text == 'Thanks pricing'

        retention = CreatorVaultRetention(x.repository.database, clock=lambda: x.f.clock.now)
        operation_id = None
        if retirement == 'message':
            retention.delete_message(ACCOUNT, HIDDEN)
        elif retirement == 'account':
            operation_id = str(uuid4())
            retention.delete_all(ACCOUNT, provenance='creator_delete:' + operation_id)
        else:
            x.f.clock.now += timedelta(minutes=6)
            sweep = await asyncio.to_thread(
                RetentionMaintenance(x.repository.database, clock=lambda: x.f.clock.now).run_once
            )
            assert sweep.accounts_checked == 1 and sweep.expired_message_count == 1
        barriers = retention.barriers(ACCOUNT)
        assert barriers
        with x.repository.database.read() as db:
            assert not db.execute('SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id=?',
                                  (ACCOUNT, HIDDEN)).fetchone()
            assert db.execute('SELECT count(*) FROM account_messages WHERE creator_account_id=?',
                              (ACCOUNT,)).fetchone()[0] == (0 if retirement == 'account' else 1)
        assert_preview_erased(x, job)
        survivors = contribution_bytes(x)
        assert len(survivors) == (0 if retirement == 'account' else 1)
        assert set(survivors) <= set(original_members)
        restore_authoritative_backup(offline(x), backup, now=x.f.clock.now)
        assert retention.barriers(ACCOUNT) == barriers
        assert_preview_erased(x, job)
        assert contribution_bytes(x) == survivors

        async with (
            configured_preview_runtime(x, path) as (_, service),
            deletion_owner(x) as (operations, deletions),
        ):
            # Resume the same bounded maintenance/deletion ports after the canonical restore.
            await asyncio.to_thread(
                RetentionMaintenance(x.repository.database, clock=lambda: x.f.clock.now,
                                     on_source_change=runtime.invalidate_question_sources).run_once
            )
            with lifecycle_http(x, service) as client:
                result = await asyncio.to_thread(client.get, PREVIEWS + '/' + body.external_key)
                assert result.status_code == 200, result.text
                deleted = result.json()
                assert deleted['receipt']['state'] == 'result_deleted'
                assert deleted['receipt']['job_id'] == job and deleted['receipt']['contribution_id'] == contribution
                assert deleted['scope'] is None and deleted['confirmation'] is None and deleted['disclosure'] is None
                assert (await post(client, PREVIEWS, body.model_dump(mode='json'))) == deleted
                assert (await post(client, PREVIEWS + '/' + body.external_key + '/confirm', prepared['confirmation'])) == deleted
                unavailable = await asyncio.to_thread(client.get, PREVIEWS + '/results/' + contribution)
                assert unavailable.status_code == 409
                with pytest.raises(ValueError, match='output_unavailable'):
                    service.read_saved_evidence(x.http_policy(), contribution, saved.summaries[0].evidence[0].reference)
                replay = await post(client, EXECUTION, enable.model_dump(mode='json'))
                assert replay['receipt'] == enabled['receipt']
                assert replay['current']['state'] == ('retired' if retirement == 'account' else 'paused')
                for intent in (ClientIntentPut(kind='preview_prepare', key=body.external_key, body=body),
                               ClientIntentPut(kind='preview_confirm', key=body.external_key, body=confirm_intent)):
                    response = await asyncio.to_thread(client.get, INTENTS + '/' + intent.kind + '/' + intent.key)
                    assert response.status_code == 200 and response.json()['body'] == intent.body.model_dump(mode='json')
            assert_preview_erased(x, job)
            def current_records_only():
                with service.dependencies.findings.database.read() as db:
                    documents = sorted(bytes(row[0]) for row in db.execute('SELECT document FROM nlp_records'))
                return documents == sorted(bytes(row[2]) for row in survivors)

            async with asyncio.timeout(30):
                while True:
                    await deletions.run_pending()
                    with x.repository.database.read() as db:
                        work = db.execute('SELECT target_revision,completed_revision FROM derived_deletion_work '
                                          'WHERE creator_account_id=?', (ACCOUNT,)).fetchone()
                    if work is not None and work[0] == work[1] and current_records_only():
                        break
                    await asyncio.sleep(.02)
            if operation_id is not None:
                assert operations.get(ACCOUNT, operation_id).status == 'complete'
            with service.dependencies.findings.database.read() as db:
                assert not db.execute('SELECT 1 FROM nlp_record_origins WHERE contribution_id=?', (contribution,)).fetchone()
            assert contribution_bytes(x) == survivors
            assert rows(x.repository.database, 'text_analysis_client_intents') == journal
            assert rows(x.repository.database, 'text_analysis_enrollment_commands') == commands
            assert rows(x.repository.database, 'text_analysis_preview_requests') == requests
            assert rows(x.repository.database, 'text_analysis_preview_admissions') == admissions
            assert tuple(x.analyzed) == calls
    finally:
        close_preview_fixture(x)
