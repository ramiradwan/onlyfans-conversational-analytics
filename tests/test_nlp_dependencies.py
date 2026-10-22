"""Invented releases through encrypted authority, real data storage and publication."""
from contextlib import contextmanager
from dataclasses import replace
import copy
import hashlib
import json
import os
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.analytics.nlp_findings_store import FindingsStore
from app.nlp import adapter
from app.nlp.artifact_store import ArtifactStore, StoreLimits
from app.nlp.profiles import ResourcePolicy, canonical_bytes
from app.nlp.release_catalog import canonical, verify_catalog, CATALOG_TYPE, DependencyRejected
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.nlp_dependencies import DependencyPersistence
from app.services.nlp_dependencies import DependencyService
from app.services.nlp_distribution import ProfileDistribution
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup, advance
from tests.test_nlp_artifact_store import example
from tests.test_nlp_profile_authentication import keys, sign
from tests.test_nlp_processing_publication import ConformanceAdapter

pytestmark = pytest.mark.ci_tier('integration')


class Owners:
    def __init__(self):
        self.birth = 'one'

    def identity(self, pid):
        return {'pid': pid, 'birth': self.birth}

    def absent(self, owner):
        return owner != self.identity(owner['pid'])


class Engine:
    def __init__(self):
        self.present, self.prepares, self.before_prepare = True, 0, None
        self.events = []

    def current(self, engine):
        return self.present

    @contextmanager
    def open(self, profile, available, check, *, deadline=None):
        owner = self

        class Session:
            def handshake(self, request, *, deadline=None):
                caps = tuple(adapter.AdapterCapability(adapter=c.adapter,
                    task_contracts=tuple(t.task_contract for t in c.tasks),
                    score_kinds=tuple(dict.fromkeys(t.score_policy.kind for t in c.tasks)),
                    max_source_codepoints=c.max_source_codepoints, max_batch_items=c.max_batch_items,
                    max_windows=c.max_windows, max_candidates=c.max_candidates,
                    max_output_findings=c.max_output_findings,
                    runtime_requirements_sha256=c.runtime_requirements.sha256) for c in profile.components)
                return adapter.WorkerHandshake(request_id=request.request_id, engine=request.expected_engine,
                                               adapters=caps).model_dump_json().encode()

            def prepare(self, request, *, deadline=None, cancelled=None):
                owner.prepares += 1
                owner.events.append(('prepare', request.component_sha256))
                if owner.before_prepare:
                    owner.before_prepare()
                check()
                return adapter.PreparationResult(**{k: getattr(request, k) for k in
                    ('request_id', 'profile_sha256', 'component_id', 'component_sha256')},
                    outcome='ready', reason='none').model_dump_json().encode()

            def unload(self, request, *, deadline=None):
                owner.events.append(('unload', request.component_sha256))
                return adapter.LifecycleResult(operation='unload', request_id=request.request_id,
                    profile_sha256=request.profile_sha256, component_sha256=request.component_sha256,
                    outcome='completed', health='unloaded').model_dump_json().encode()

            def analyze(self, request, cancelled, *, deadline=None):
                return ConformanceAdapter(profile)(request, cancelled)

        yield Session()

    def drained(self, profile):
        return True

    def close(self, timeout=5):
        return True


class Environment:
    def __init__(self, tmp_path, kind='joint'):
        self.root = tmp_path
        self.fixture = make_fixture(tmp_path, conversations=1, messages=2)
        self.profile, payloads = example(kind)
        self.approved = {self.profile.identity_digest()}
        self.engine, self.owners = Engine(), Owners()
        self.clock = SimpleNamespace(now=1000)
        self.requests, self.hook = [], None
        self.objects = {'profiles/' + self.profile.identity_digest(): sign(canonical_bytes(self.profile))}
        self.objects.update({'data/' + hashlib.sha256(raw).hexdigest(): raw for raw in payloads.values()})
        self.persistence = DependencyPersistence(SQLiteAuthenticationStore(tmp_path / 'auth.sqlite3'))
        self.artifacts = ArtifactStore(tmp_path / 'artifacts', StoreLimits(1024 * 1024, 4 * 1024 * 1024, 0))
        self.findings = FindingsStore(self.fixture.stores.database)
        self.policy = ResourcePolicy(download_bytes=1024 * 1024, installed_bytes=4 * 1024 * 1024,
            temporary_bytes=4 * 1024 * 1024, transition_peak_memory_bytes=1024 * 1024, inference_threads=2)

        def respond(request):
            relative = request.url.path.removeprefix('/nlp/')
            self.requests.append(relative)
            if self.hook:
                self.hook(relative)
            return httpx.Response(200, stream=httpx.ByteStream(self.objects[relative]))

        self.distribution = ProfileDistribution('https://example.invalid/nlp/', transport=httpx.MockTransport(respond))
        self.service = self.construct()
        self.service.start()
        self.service.learn(self.catalog())

    def construct(self):
        return DependencyService(self.persistence, self.artifacts, self.findings, self.fixture.source,
            self.distribution, keys=keys(), approvals=lambda p: p.identity_digest() in self.approved,
            engine_provider=self.engine, resource_policy=self.policy, spool_root=self.root / 'spool',
            clock=lambda: self.clock.now, owners=self.owners)

    def catalog(self, *, sequence=1, withdrawals=(), expires=2000, profiles=True):
        p = self.profile
        doc = {'schema_version': 'nlp-release-catalog.v1', 'sequence': sequence,
            'issued_at': 900, 'expires_at': expires, 'profiles': [] if not profiles else [{
                'profile_id': p.profile_id, 'revision': p.revision, 'profile_sha256': p.identity_digest(),
                'compatibility': {'profile_sha256': p.identity_digest(),
                    'compatible_engines': [p.components[0].engine.model_dump(mode='json')],
                    'adapters': [c.adapter.model_dump(mode='json') for c in p.components]}}],
            'withdrawals': list(withdrawals)}
        return sign(canonical(doc), typ=CATALOG_TYPE)

    def acquire(self):
        self.last_plan = self.service.plan(self.profile.profile_id, self.profile.revision)['operation_id']
        op = self.service.acquire(self.profile.profile_id, self.profile.revision, plan_id=self.last_plan)
        self.service.thread.join(10)
        assert not self.service.thread.is_alive()
        return op

    def selected(self):
        op = self.acquire()
        assert self.service._operation(op)['state'] == 'complete'
        self.service.select(ACCOUNT, self.profile.profile_id, self.profile.revision)
        return op


def test_bound_process_session_is_used_without_adapter_override(env):
    from app.analytics.nlp_processing import FindingsRunner
    e = env
    e.selected()
    before = e.engine.prepares
    runner = FindingsRunner(e.fixture.source, e.fixture.stores.projections, authorize=lambda _: None,
        execution_ready=e.service.ready, dependencies=e.service, adapter=None,
        clock=lambda: e.fixture.clock.now, enrichment=e.fixture.pipeline.enrichment)
    assert runner.process_one(ACCOUNT)
    assert e.engine.prepares == before + len(e.profile.components)
    assert e.engine.events[-1][0] == 'unload'


def test_warmup_unloads_each_serial_component(tmp_path):
    e = Environment(tmp_path, 'split')
    try:
        e.selected()
        expected = [event for component in e.profile.components for event in
            [('prepare', component.identity_digest()), ('unload', component.identity_digest())]]
        assert e.engine.events == expected
    finally:
        assert e.service.close()
        cleanup(e.fixture)


def test_close_tracks_synchronous_prepare_and_closes_readiness(env):
    e = env
    e.selected()
    entered, release, errors = threading.Event(), threading.Event(), []

    def block():
        entered.set()
        assert release.wait(3)

    def prepare():
        try:
            e.service.prepare_installed(e.profile.identity_digest())
        except ValueError:
            errors.append('stopped')

    e.engine.before_prepare = block
    thread = threading.Thread(target=prepare)
    thread.start()
    assert entered.wait(3)
    try:
        assert not e.service.close(0)
        assert not e.service.ready(e.profile)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and errors == ['stopped']
    assert e.service.close()


def test_failed_process_drain_keeps_durable_pin(env):
    e = env
    e.selected()
    e.engine.drained = lambda _: False
    with e.service.retain(e.profile):
        pass
    pins = [op for op in e.persistence.operations() if op['kind'] == 'pin' and op['state'] == 'running']
    assert len(pins) == 1
    assert not e.service.close(0)
    assert e.service.pins
    e.engine.drained = lambda _: True
    assert e.service.close()
    assert not e.service.pins
    assert e.service._operation(pins[0]['id'])['state'] == 'complete'


@pytest.fixture
def env(tmp_path):
    e = Environment(tmp_path)
    yield e
    assert e.service.close()
    cleanup(e.fixture)


@pytest.mark.parametrize('kind', ['joint', 'split'])
def test_signed_acquisition_storage_selection_and_both_task_publication(tmp_path, kind):
    from app.analytics.nlp_processing import FindingsRunner
    e = Environment(tmp_path, kind)
    try:
        e.selected()
        r = FindingsRunner(e.fixture.source, e.fixture.stores.projections, authorize=lambda a: None,
            execution_ready=e.service.ready, dependencies=e.service, adapter=ConformanceAdapter(e.profile),
            clock=lambda: e.fixture.clock.now, enrichment=e.fixture.pipeline.enrichment)
        r.reconcile(ACCOUNT)
        assert r.process_one(ACCOUNT)
        assert r.process_one(ACCOUNT)
        result = r.publish_pending(ACCOUNT)
        assert result.changed
        assert len(e.service.available) == 1
        assert e.findings.state(ACCOUNT)['selection_epoch'] == 1
        e.acquire()
        assert len([x for x in e.requests if x.startswith('data/')]) == len(e.profile.artifacts) - 2
    finally:
        assert e.service.close()
        cleanup(e.fixture)


def test_catalog_replay_equivocation_withdrawal_and_unknown_fields(env):
    e = env
    e.selected()
    before = e.persistence.authority()
    e.service.learn(e.catalog())
    assert e.persistence.authority() == before
    with pytest.raises(ValueError):
        e.service.learn(e.catalog(expires=2500))
    e.service.learn(e.catalog(sequence=2, withdrawals=[e.profile.identity_digest()]))
    assert not e.service.ready(e.profile)
    once = e.findings.state(ACCOUNT)
    e.service.learn(e.catalog(sequence=2, withdrawals=[e.profile.identity_digest()]))
    e.service.reconcile()
    assert e.findings.state(ACCOUNT) == once
    with pytest.raises(ValueError):
        e.service.acquire(e.profile.profile_id, e.profile.revision, plan_id=e.last_plan)


def test_expiry_allows_accepted_offline_but_blocks_new_selection(env):
    env.selected()
    env.clock.now = 2001
    assert env.service.ready(env.profile)
    with pytest.raises(ValueError):
        env.service.select(ACCOUNT, env.profile.profile_id, env.profile.revision)


def test_clock_high_water_advances_on_selection_and_refuses_rollback(env):
    env.acquire()
    env.clock.now = 1500
    env.service.select(ACCOUNT, env.profile.profile_id, env.profile.revision)
    assert env.persistence.authority()['clock_high_water'] == 1500
    env.clock.now = 1400
    with pytest.raises(ValueError):
        env.service.select(ACCOUNT, env.profile.profile_id, env.profile.revision)


def test_restart_never_warms_automatically_and_rechecks_files(env):
    e = env
    e.selected()
    calls = list(e.requests)
    prepared = e.engine.prepares
    assert e.service.close()
    e.owners.birth = 'two'
    e.service = e.construct()
    e.service.start()
    assert e.requests == calls and e.engine.prepares == prepared
    assert not e.service.ready(e.profile)
    e.service.prepare_installed(e.profile.identity_digest())
    assert e.service.ready(e.profile)


def test_unapproved_profile_never_loads_or_downloads_data(env):
    e = env
    e.approved.clear()
    with pytest.raises(ValueError):
        e.acquire()
    assert e.engine.prepares == 0 and not any(p.startswith('data/') for p in e.requests)


def test_withdrawal_during_prepare_retains_no_ready_profile(env):
    e = env
    e.engine.before_prepare = lambda: e.service.learn(e.catalog(sequence=2, withdrawals=[e.profile.identity_digest()]))
    op = e.acquire()
    assert e.service._operation(op)['state'] == 'interrupted'
    assert not e.service.ready(e.profile)


def test_retention_pin_does_not_block_withdrawal_and_prevents_removal(env):
    e = env
    e.selected()
    directory = e.service.available[e.profile.identity_digest()].directory
    with e.service.retain(e.profile):
        e.service.learn(e.catalog(sequence=2, withdrawals=[e.profile.identity_digest()]))
        assert e.persistence.authority()['withdrawals'] == [e.profile.identity_digest()]
        assert e.service.remove(ACCOUNT)['removed_closures'] == 0
        assert directory.exists()
    assert not directory.exists()


def test_late_selection_intent_cannot_cross_source_or_selection_epoch(env):
    e = env
    e.acquire()
    op = {'id': 'a' * 32, 'kind': 'select', 'state': 'pending', 'account': ACCOUNT,
          'source_token': e.service._source_token(ACCOUNT), 'expected': e.findings.selection_token(ACCOUNT),
          'profile': e.profile.identity_digest()}
    e.persistence.operation(op)
    with e.fixture.repositories.database.transaction() as db:
        advance(db)
    e.service.reconcile()
    assert e.service._operation(op['id'])['state'] == 'refused'
    with pytest.raises(ValueError):
        e.findings.state(ACCOUNT)


def test_default_public_surface_is_unavailable_and_side_effect_free():
    from app.services.nlp_dependencies import dependency_service
    assert dependency_service() is None


def test_cost_plan_precedes_data_and_reports_actual_closure_reuse(env):
    e = env
    first = e.service.plan(e.profile.profile_id, e.profile.revision)
    assert not any(p.startswith('data/') for p in e.requests)
    assert e.engine.prepares == 0
    assert first['incremental_data_download_bytes'] == first['data_download_bytes'] > 0
    assert first['profile_metadata_bytes'] > 0
    e.acquire()
    later = e.service.plan(e.profile.profile_id, e.profile.revision)
    assert later['incremental_data_download_bytes'] == 0
    assert later['acquisition_temporary_bytes'] == 0
    assert later['data_download_bytes'] == first['data_download_bytes']


def test_verified_cache_reuse_does_not_charge_missing_data_space(env, monkeypatch):
    e = env
    e.acquire()
    data_requests = [p for p in e.requests if p.startswith('data/')]
    # Actual store re-verification still runs; only the observed free space changes.
    monkeypatch.setattr('shutil.disk_usage', lambda path: SimpleNamespace(free=1))
    op = e.acquire()
    assert e.service.observation(op)['state'] == 'complete'
    assert [p for p in e.requests if p.startswith('data/')] == data_requests


def test_missing_closure_keeps_full_temporary_reserve(env, monkeypatch):
    e = env
    quote = e.service.plan(e.profile.profile_id, e.profile.revision)
    assert quote['acquisition_temporary_bytes'] > quote['data_download_bytes']
    monkeypatch.setattr('shutil.disk_usage', lambda path: SimpleNamespace(free=1))
    op = e.service.acquire(e.profile.profile_id, e.profile.revision, plan_id=quote['operation_id'])
    e.service.thread.join(10)
    assert e.service.observation(op)['state'] == 'interrupted'
    assert not any(p.startswith('data/') for p in e.requests)


def test_cached_status_and_account_controls_have_separate_meanings(env):
    e = env
    before = e.persistence.authority()
    requests = list(e.requests)
    status = e.service.status(ACCOUNT)
    assert status['selected'] is None
    assert status['profiles'][0]['profile_id'] == e.profile.profile_id
    assert status['profiles'][0]['languages'] is None
    assert e.requests == requests and e.persistence.authority() == before
    e.selected()
    e.service.account_mode(ACCOUNT, 'paused')
    status = e.service.status(ACCOUNT)
    assert status['selected']['mode'] == 'paused'
    assert status['profiles'][0]['installed'] and status['profiles'][0]['prepared']
    assert set(status['profiles'][0]['tasks']) == {'message_topics', 'concept_mentions'}
    e.clock.now = 2001
    e.service.account_mode(ACCOUNT, 'enabled')
    assert e.findings.state(ACCOUNT)['mode'] == 'enabled'
    e.service.remove(ACCOUNT)
    with pytest.raises(ValueError):
        e.service.account_mode(ACCOUNT, 'enabled')
    assert e.findings.state(ACCOUNT)['mode'] == 'removed'
    assert 'profile_json' not in json.dumps(status)


def test_exact_dead_owner_pins_recover_without_journal_capacity_leak(env):
    e = env
    e.acquire()
    for i in range(25):
        e.persistence.operation({'id': f'{i:032x}', 'kind': 'pin', 'state': 'running',
            'owner': e.service.owner, 'profile': e.profile.identity_digest()})
    assert e.service.close()
    e.owners.birth = 'next'
    e.service = e.construct()
    e.service.start()
    assert all(op['state'] == 'complete' for op in e.persistence.operations() if op['kind'] == 'pin')
    for i in range(40):
        e.persistence.operation({'id': f'{i+100:032x}', 'kind': 'pin', 'state': 'complete',
            'owner': e.service.owner, 'profile': e.profile.identity_digest()})
    assert len(e.persistence.operations()) <= 32


@pytest.mark.parametrize('partial_cleanup', [False, True])
def test_cancelled_before_worker_finally_recovers_only_dead_owned_files(env, monkeypatch, partial_cleanup):
    from app.persistence.private_files import apply_private_directory_security, apply_private_file_security, private_file_identity
    e = env
    monkeypatch.setattr(e.service, '_launch', lambda op: None)
    quote = e.service.plan(e.profile.profile_id, e.profile.revision)
    op_id = e.service.acquire(e.profile.profile_id, e.profile.revision, plan_id=quote['operation_id'])
    e.service.spool.mkdir()
    apply_private_directory_security(e.service.spool)
    directory = e.service.spool / op_id
    directory.mkdir()
    apply_private_directory_security(directory)
    member = directory / ('f'*64 + '.part')
    member.write_bytes(b'incomplete owned data')
    apply_private_file_security(member)
    op = e.service._operation(op_id)
    op.update(directory_identity=list(e.service._directory_identity(directory)),
              spool_identity=list(e.service._directory_identity(e.service.spool)),
              files={member.name: list(private_file_identity(member))})
    e.persistence.operation(op)
    e.service.pause(op_id, cancel=True)
    assert directory.exists() and e.service.observation(op_id)['in_flight']
    live = e.construct()
    with pytest.raises(ValueError, match='owner_live'):
        live.start()
    assert member.read_bytes() == b'incomplete owned data'
    if partial_cleanup:
        other = directory / ('e'*64 + '.part')
        other.write_bytes(b'second owned member')
        apply_private_file_security(other)
        op = e.service._operation(op_id)
        op['files'][other.name] = list(private_file_identity(other))
        e.persistence.operation(op)
        real_unlink = type(member).unlink
        deleted = []

        def interrupt(path, *args, **kwargs):
            if path.parent == directory:
                if deleted:
                    raise OSError('invented cleanup interruption')
                deleted.append(path)
            return real_unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(type(member), 'unlink', interrupt)
            with pytest.raises(OSError):
                e.service._clean_spool(directory, op)
        assert len(list(directory.iterdir())) == 1 and len(deleted) == 1
    e.owners.birth = 'after-crash'
    e.service = e.construct()
    e.service.start()
    assert not directory.exists()
    result = e.service.observation(op_id)
    assert result['state'] == 'cancelled' and not result['in_flight']


def test_removed_metadata_is_reclaimed_beyond_thirty_two_revisions(env):
    from app.nlp.profiles import AnalysisProfile, artifact_closure_digest
    e = env
    before = e.persistence.authority()
    for index in range(40):
        doc = e.profile.model_dump(mode='json')
        doc['revision'] = str(index + 10)
        profile = AnalysisProfile.model_validate_json(canonical(doc))
        closure = artifact_closure_digest(tuple(a for a in profile.artifacts if a.role in ('weights', 'tokenizer', 'configuration')))
        with e.service.gate:
            e.persistence.install(profile, closure, planned=True)
            e.persistence.removed(profile.identity_digest())
        assert len(e.persistence.profiles()) <= 1
    assert e.persistence.authority() == before


def test_catalog_rotation_does_not_hide_installed_selected_profile(env):
    e = env
    e.selected()
    document = e.service.catalog.model_dump(mode='json')
    template = document['profiles'][0]
    entries = []
    for index in range(32):
        entry = copy.deepcopy(template)
        entry['revision'] = f'next.{index}'
        entry['profile_sha256'] = hashlib.sha256(str(index).encode()).hexdigest()
        entry['compatibility']['profile_sha256'] = entry['profile_sha256']
        entries.append(entry)
    document.update(sequence=2, profiles=entries)
    e.service.learn(sign(canonical(document), typ=CATALOG_TYPE))
    status = e.service.status(ACCOUNT)
    assert len(status['profiles']) == 33
    installed = next(p for p in status['profiles'] if p['profile_sha256'] == e.profile.identity_digest())
    assert installed['installed'] and installed['prepared'] and not installed['catalogued']
    assert status['selected']['profile_sha256'] == installed['profile_sha256']


@pytest.mark.asyncio
async def test_optional_startup_failure_preserves_authority_and_base_startup(env, monkeypatch, caplog):
    import app.main as main
    import app.services.nlp_dependencies as module
    e = env
    calls = []
    assert e.service.close()
    e.service = e.construct()
    with e.persistence.database.transaction() as db:
        db.execute("UPDATE nlp_dependency_authority SET state_sha256=?", ('f'*64,))
    with e.persistence.database.read() as db:
        before = tuple(db.execute('SELECT * FROM nlp_dependency_authority').fetchone())

    async def connect():
        calls.append('broadcast')

    async def start_transport():
        calls.append('transport')

    monkeypatch.setattr(module, '_service', e.service)
    monkeypatch.setattr(main.broadcast, 'connect', connect)
    monkeypatch.setattr(main.transport_manager, 'start', start_transport)
    monkeypatch.setattr(main.analytics_runtime, 'launch_default_analytics_runtime', lambda: calls.append('analytics'))
    await main.startup_event()
    assert calls == ['broadcast', 'transport', 'analytics']
    with e.persistence.database.read() as db:
        assert tuple(db.execute('SELECT * FROM nlp_dependency_authority').fetchone()) == before
    assert e.service.status(ACCOUNT)['failure'] == 'nlp_dependency_unavailable'
    assert not e.service.ready(e.profile)
    with pytest.raises(ValueError):
        e.service.refresh()
    assert 'private diagnostic' not in caplog.text


@pytest.mark.asyncio
async def test_optional_undrained_worker_does_not_skip_core_shutdown(env, monkeypatch):
    import app.main as main
    import app.services.nlp_dependencies as module
    import app.services.retention_maintenance as maintenance
    calls = []

    async def stop():
        calls.append('transport')

    async def scheduler(**kwargs):
        calls.append('analytics')
        return True

    async def disconnect():
        calls.append('broadcast')

    async def maintenance_stop():
        return True

    monkeypatch.setattr(module, '_service', SimpleNamespace(close=lambda: False))
    monkeypatch.setattr(maintenance, 'shutdown_default_retention_maintenance', maintenance_stop)
    monkeypatch.setattr(main.transport_manager, 'stop', stop)
    monkeypatch.setattr(main.analytics_runtime, 'shutdown_default_analytics_runtime', scheduler)
    monkeypatch.setattr(main.broadcast, 'disconnect', disconnect)
    await main.shutdown_event()
    assert calls == ['transport', 'analytics', 'broadcast']
    assert not main.app.state.runtime_shutdown_drained


def test_pause_retains_only_verified_files_and_restarts_interrupted_file(env):
    e = env
    seen = []

    def pause_second(relative):
        if relative.startswith('data/'):
            seen.append(relative)
            if len(seen) == 2:
                op = next(o for o in e.persistence.operations() if o['kind'] == 'acquire' and o['state'] == 'running')
                e.service.pause(op['id'])

    e.hook = pause_second
    op_id = e.acquire()
    paused = e.service.observation(op_id)
    assert paused['state'] == 'paused' and paused['verified_files'] == 1
    assert not e.service.ready(e.profile)
    e.hook = None
    e.service.resume(op_id)
    e.service.thread.join(10)
    assert e.service.observation(op_id)['state'] == 'complete'
    assert e.requests.count(seen[0]) == 1
    assert e.requests.count(seen[1]) == 2
    assert e.service.ready(e.profile)


def test_cancel_removes_only_journaled_spool_and_accepts_no_partial_artifact(env):
    e = env

    def cancel(relative):
        if relative.startswith('data/'):
            op = next(o for o in e.persistence.operations() if o['kind'] == 'acquire' and o['state'] == 'running')
            e.service.pause(op['id'], cancel=True)

    e.hook = cancel
    operation = e.acquire()
    assert e.service.observation(operation)['state'] == 'cancelled'
    assert not (e.service.spool / operation).exists()
    assert not e.service.ready(e.profile)
    assert not any(e.artifacts.root.iterdir())


def test_bad_hash_retains_no_accepted_artifact_and_explicit_resume_restarts_file(env):
    e = env
    path = next(p for p in e.objects if p.startswith('data/'))
    good = e.objects[path]
    e.objects[path] = b'x' * len(good)
    operation = e.acquire()
    assert e.service.observation(operation)['state'] == 'interrupted'
    assert not e.service.ready(e.profile)
    e.objects[path] = good
    e.service.resume(operation)
    e.service.thread.join(10)
    assert e.service.observation(operation)['state'] == 'complete'


def test_foreign_spool_member_refuses_resume_without_cleanup(env):
    e = env

    def pause(relative):
        if relative.startswith('data/'):
            op = next(o for o in e.persistence.operations() if o['kind'] == 'acquire' and o['state'] == 'running')
            e.service.pause(op['id'])

    e.hook = pause
    operation = e.acquire()
    foreign = e.service.spool / operation / 'foreign'
    foreign.write_bytes(b'preserve')
    e.hook = None
    e.service.resume(operation)
    e.service.thread.join(10)
    assert e.service.observation(operation)['state'] == 'interrupted'
    assert foreign.read_bytes() == b'preserve'


def test_remove_then_plan_and_reacquire_same_exact_profile(env):
    e = env
    e.selected()
    assert e.service.remove(ACCOUNT)['removed_closures'] == 1
    assert not e.service.ready(e.profile)
    operation = e.acquire()
    assert e.service.observation(operation)['state'] == 'complete'
    assert e.service.ready(e.profile)


def test_delete_after_preliminary_source_check_cannot_commit_selection(env, monkeypatch):
    from app.persistence.retention import CreatorVaultRetention
    e = env
    e.acquire()
    real = e.fixture.source.dependency_selection_guard

    @contextmanager
    def race(account, expected):
        CreatorVaultRetention(e.fixture.repositories.database,
            clock=lambda:e.fixture.clock.now).delete_all(account)
        with real(account, expected):
            yield

    monkeypatch.setattr(e.fixture.source, 'dependency_selection_guard', race)
    operation = e.service.select(ACCOUNT, e.profile.profile_id, e.profile.revision)
    assert e.service.observation(operation)['state'] == 'refused'
    with pytest.raises(ValueError):
        e.findings.state(ACCOUNT)


def test_withdrawal_after_build_blocks_final_publication(env):
    from app.analytics.nlp_processing import FindingsRunner
    e = env
    e.selected()
    r = FindingsRunner(e.fixture.source, e.fixture.stores.projections, authorize=lambda a:None,
        execution_ready=e.service.ready, dependencies=e.service, adapter=ConformanceAdapter(e.profile),
        clock=lambda:e.fixture.clock.now, enrichment=e.fixture.pipeline.enrichment)
    r.reconcile(ACCOUNT)
    assert r.process_one(ACCOUNT)
    pipeline = r.frozen_pipeline(ACCOUNT)
    candidate = pipeline.build_candidate(ACCOUNT)
    e.service.learn(e.catalog(sequence=2, withdrawals=[e.profile.identity_digest()]))
    with pytest.raises(ValueError):
        pipeline.publish_candidate(candidate)


@pytest.mark.parametrize('status', [302, 404, 500])
def test_distribution_refuses_redirect_and_error_bodies(status):
    d = ProfileDistribution('https://example.invalid/nlp/', transport=httpx.MockTransport(
        lambda request:httpx.Response(status, stream=httpx.ByteStream(b'opaque'))))
    with pytest.raises(ValueError):
        d.read('catalog.jws', 100, check=lambda:None, deadline=d.clock()+10)


def test_distribution_refuses_overflow_and_other_origins():
    d = ProfileDistribution('https://example.invalid/nlp/', transport=httpx.MockTransport(
        lambda request:httpx.Response(200, stream=httpx.ByteStream(b'x'*101))))
    with pytest.raises(ValueError):
        d.read('catalog.jws', 100, check=lambda:None, deadline=d.clock()+10)
    with pytest.raises(ValueError):
        d.read('https://other.invalid/catalog.jws', 100, check=lambda:None, deadline=d.clock()+10)


@pytest.mark.parametrize('fault', ['type', 'signer', 'duplicate', 'noncanonical', 'extra', 'too_many'])
def test_catalog_rejects_domain_signer_parser_and_shape(fault, env):
    e = env
    raw = e.catalog()
    import base64
    doc = json.loads(base64.urlsafe_b64decode(raw.split(b'.')[1] + b'=='))
    if fault == 'type':
        raw = sign(canonical(doc))
    elif fault == 'signer':
        from cryptography.hazmat.primitives.asymmetric import ec
        raw = sign(canonical(doc), typ=CATALOG_TYPE, private_key=ec.derive_private_key(20,ec.SECP256R1()))
    elif fault == 'duplicate':
        raw = sign(canonical(doc)[:-1] + b',"sequence":1}', typ=CATALOG_TYPE)
    elif fault == 'noncanonical':
        raw = sign(json.dumps(doc,indent=2).encode(), typ=CATALOG_TYPE)
    else:
        if fault == 'extra':
            doc['qualified'] = True
        else:
            doc['profiles'] *= 33
        raw = sign(canonical(doc),typ=CATALOG_TYPE)
    with pytest.raises(ValueError):
        verify_catalog(raw, keys())


@pytest.mark.parametrize('withdraw_previous', [False, True])
def test_reselect_current_preserves_distinct_rollback_and_its_artifacts(env, withdraw_previous):
    from app.nlp.profiles import AnalysisProfile
    e = env
    e.selected()
    previous = e.profile
    profile, payloads = example('split')
    document = profile.model_dump(mode='json')
    document['revision'] = 'next'
    e.profile = AnalysisProfile.model_validate_json(canonical(document))
    e.approved.add(e.profile.identity_digest())
    e.objects['profiles/' + e.profile.identity_digest()] = sign(canonical_bytes(e.profile))
    e.objects.update({'data/' + hashlib.sha256(raw).hexdigest(): raw for raw in payloads.values()})
    e.service.learn(e.catalog(sequence=2))
    e.selected()
    before = e.findings.state(ACCOUNT)
    assert AnalysisProfile.model_validate_json(before['previous_profile_json']) == previous
    from app.analytics.nlp_processing import FindingsRunner
    runner = FindingsRunner(e.fixture.source, e.fixture.stores.projections, authorize=lambda account: None,
        execution_ready=e.service.ready, adapter=ConformanceAdapter(e.profile), clock=lambda: e.fixture.clock.now)
    runner.reconcile(ACCOUNT)
    claim = e.findings.claim(ACCOUNT)
    assert claim is not None and e.findings.valid_claim(claim)
    before = e.findings.state(ACCOUNT)
    e.service.select(ACCOUNT, e.profile.profile_id, e.profile.revision)
    repeated = e.findings.state(ACCOUNT)
    assert repeated['selection_epoch'] == before['selection_epoch'] + 1
    assert repeated['admission_epoch'] == before['admission_epoch'] + 1
    assert repeated['availability_revision'] == before['availability_revision'] + 1
    assert not e.findings.valid_claim(claim)
    with e.fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_jobs').fetchone()[0] == 0
    assert AnalysisProfile.model_validate_json(repeated['previous_profile_json']) == previous
    # A requested collection cannot remove the retained rollback closure.
    e.service.remove(ACCOUNT, previous.profile_id, previous.revision)
    assert e.artifacts.reopen(previous).data_closure_sha256 != e.artifacts.reopen(e.profile).data_closure_sha256
    assert any(p.identity_digest() == previous.identity_digest() and state == 'available'
               for p, _, state in e.persistence.profiles())
    if withdraw_previous:
        e.service.learn(e.catalog(sequence=3, withdrawals=[previous.identity_digest()]))
        with pytest.raises(ValueError):
            e.service.select(ACCOUNT, None, None, rollback=True)
        assert AnalysisProfile.model_validate_json(e.findings.state(ACCOUNT)['profile_json']) == e.profile
    else:
        e.service.select(ACCOUNT, None, None, rollback=True)
        selected = e.findings.state(ACCOUNT)
        assert AnalysisProfile.model_validate_json(selected['profile_json']) == previous
        assert AnalysisProfile.model_validate_json(selected['previous_profile_json']) == e.profile


def test_reselect_first_profile_does_not_create_self_rollback(env):
    e = env
    e.selected()
    before = e.findings.state(ACCOUNT)
    assert before['previous_profile_json'] is None
    with e.fixture.stores.database.transaction() as db:
        db.execute('UPDATE nlp_accounts SET profile_json=?',
                   (json.dumps(json.loads(before['profile_json']), indent=2),))
    e.service.select(ACCOUNT, e.profile.profile_id, e.profile.revision)
    selected = e.findings.state(ACCOUNT)
    assert selected['selection_epoch'] == before['selection_epoch'] + 1
    assert selected['previous_profile_json'] is None
    with pytest.raises(ValueError):
        e.service.select(ACCOUNT, None, None, rollback=True)
