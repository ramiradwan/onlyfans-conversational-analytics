"""Synthetic inputs preserve real contracts and disposable-store ownership."""
import ast
import asyncio
import hashlib
import importlib.util
from io import BytesIO
import json
from itertools import islice
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.nlp import v2
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.text_analysis_identity import TextAnalysisIdentityOwner

pytestmark = pytest.mark.ci_tier('integration')

SOURCE = Path(__file__).resolve().parents[1]/'tools/e2e-capture/helpers/text_analysis_fixture.py'
spec = importlib.util.spec_from_file_location('e2e_text_analysis_fixture', SOURCE)
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)


def configuration(tmp_path):
    root = tmp_path/'ofca-text-analysis-inert'
    root.mkdir()
    return SimpleNamespace(auth_database_path=root/'auth.sqlite3', canonical_database_path=root/'canonical.sqlite3',
        projection_database_path=root/'projections.sqlite3', analytics_projection_database_path=root/'analytics-projections.sqlite3',
        websocket_auth_mode='local_session', identity_binding_source='verified_grants')


def profile(scenario):
    owner = fixture.FixtureOwner.__new__(fixture.FixtureOwner)
    owner.scenario = scenario
    return owner._profile()[0]


def request(selected, component):
    return v2.WorkerRequest(request_id='fixture-request', profile_sha256=selected.identity_digest(),
        component_id=component.component_id, component_sha256=component.identity_digest(),
        tasks=tuple(task.task for task in component.tasks), deadline_ms=5000,
        items=tuple(v2.WorkerInput(request_item_id=row['message_id'], text=row['text'],
            text_sha256=hashlib.sha256(row['text'].encode()).hexdigest()) for row in fixture.source_fixture()['messages']))


def shutdown_listener_type(phases):
    entry = SOURCE.with_name('brain_entry.py')
    tree = ast.parse(entry.read_text(encoding='utf-8'), filename=str(entry))
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == '_ShutdownListener']
    assert len(nodes) == 1
    namespace = {'_shutdown_phase': phases.append, 'asyncio': asyncio, 'sys': sys,
                 'islice': islice, 'json': json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(entry), 'exec'), namespace)
    return namespace['_ShutdownListener']


@pytest.mark.skipif(not hasattr(asyncio.Server, 'close_clients'), reason='Server.close_clients requires Python 3.13')
@pytest.mark.parametrize('queued_writes', [False, True])
def test_listener_gracefully_closes_real_attached_client_before_waiting(queued_writes, capsys):
    phases = []
    listener_type = shutdown_listener_type(phases)

    async def run():
        loop = asyncio.get_running_loop()
        accepted = loop.create_future()
        server_lost = loop.create_future()
        client_lost = loop.create_future()
        paused = loop.create_future()
        received = 0

        class ServerProtocol(asyncio.Protocol):
            def connection_made(self, transport):
                accepted.set_result(transport)

            def connection_lost(self, error):
                server_lost.set_result(error)

            def pause_writing(self):
                if not paused.done():
                    paused.set_result(None)

        class ClientProtocol(asyncio.Protocol):
            def connection_made(self, transport):
                if queued_writes:
                    transport.pause_reading()

            def data_received(self, data):
                nonlocal received
                received += len(data)

            def connection_lost(self, error):
                client_lost.set_result(error)

        server = await loop.create_server(ServerProtocol, '127.0.0.1', 0)
        client = None
        try:
            address = server.sockets[0].getsockname()
            if queued_writes:
                peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65_536)
                    peer.setblocking(False)
                    await asyncio.wait_for(loop.sock_connect(peer, address), 2)
                    client, _ = await loop.create_connection(ClientProtocol, sock=peer)
                except BaseException:
                    peer.close()
                    raise
            else:
                client, _ = await loop.create_connection(ClientProtocol, address[0], address[1])
            transport = await asyncio.wait_for(accepted, 2)
            if queued_writes:
                transport.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65_536)
                # The second write exercises queued-buffer flow control even if the first send completes immediately.
                transport.write(b'x' * (4 * 1024 * 1024))
                transport.write(b'x' * (4 * 1024 * 1024))
                await asyncio.wait_for(paused, 2)
            server.close()
            assert not server.is_serving() and not transport.is_closing()
            assert not server_lost.done()
            observed = listener_type(server)
            waiting = asyncio.create_task(observed.wait_closed())
            if queued_writes:
                await asyncio.sleep(0)
                assert transport.is_closing()
                assert not waiting.done() and not server_lost.done()
                observed._residual()
                residual = json.loads(capsys.readouterr().out)
                assert residual['schema'] == 'browser-fixture-listener-residual.v1'
                if sys.version_info[:2] == (3, 13):
                    assert residual['transports'] == 1
                    if sys.platform == 'win32':
                        assert residual['buffered'] in (0, 1)
                        assert residual == {'schema': 'browser-fixture-listener-residual.v1',
                            'transports': 1, 'closing': 1, 'pending_write': 1, 'buffered': residual['buffered'],
                            'closed_socket': 0, 'called_connection_lost': 0}
                else:
                    assert all(value is None for key, value in residual.items() if key != 'schema')
                client.resume_reading()
            result = await asyncio.wait_for(waiting, 2)
            assert result is None and transport.is_closing()
            assert server_lost.done() and server_lost.result() is None
            assert await asyncio.wait_for(client_lost, 2) is None
            assert received == (8 * 1024 * 1024 if queued_writes else 0)
            assert phases == ['server_listener_wait_entered', 'server_listener_wait_returned']
        finally:
            if client is not None:
                client.resume_reading()
                client.close()
            server.close()
            server.close_clients()
            await asyncio.wait_for(server.wait_closed(), 2)

    asyncio.run(run())


def test_listener_residual_unsupported_shape_has_only_unavailable_counts(capsys):
    shutdown_listener_type([])(object())._residual()
    value = json.loads(capsys.readouterr().out)
    assert value == {'schema': 'browser-fixture-listener-residual.v1', 'transports': None,
        'closing': None, 'pending_write': None, 'buffered': None, 'closed_socket': None,
        'called_connection_lost': None}


@pytest.mark.parametrize('close_available', [True, False, None])
def test_listener_preserves_original_wait_arguments_result_and_fallback(close_available, monkeypatch):
    phases, calls = [], []
    listener_type = shutdown_listener_type(phases)
    result, argument, keyword, attribute = object(), object(), object(), object()

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        original_call_later = loop.call_later
        observations = []

        def call_later(delay, callback, *args, **kwargs):
            handle = original_call_later(delay, callback, *args, **kwargs)
            if getattr(callback, '__name__', None) == '_residual':
                assert delay == 5
                observations.append(handle)
            return handle

        monkeypatch.setattr(loop, 'call_later', call_later)

        class Listener:
            token = attribute

            async def wait_closed(self, *args, **kwargs):
                calls.append(('wait', args, kwargs))
                entered.set()
                await release.wait()
                return result

        original = Listener()
        if close_available is True:
            original.close_clients = lambda: calls.append(('close',))
        elif close_available is None:
            original.close_clients = None
        observed = listener_type(original)
        assert observed.token is attribute
        waiting = asyncio.create_task(observed.wait_closed(argument, selected=keyword))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not waiting.done()
            assert len(observations) == 1 and not observations[0].cancelled()
            assert phases == ['server_listener_wait_entered']
            expected = [('close',)] if close_available is True else []
            assert calls == expected + [('wait', (argument,), {'selected': keyword})]
        finally:
            release.set()
            returned = await asyncio.wait_for(waiting, 2)
        assert returned is result
        assert observations[0].cancelled()
        assert phases == ['server_listener_wait_entered', 'server_listener_wait_returned']

    asyncio.run(run())


@pytest.mark.parametrize('failing_method', ['close', 'wait'])
def test_listener_preserves_original_failure_without_return_phase(failing_method):
    phases, calls = [], []
    listener_type = shutdown_listener_type(phases)
    failure = RuntimeError('Listener unavailable')

    class Listener:
        def close_clients(self):
            calls.append('close')
            if failing_method == 'close':
                raise failure

        async def wait_closed(self):
            calls.append('wait')
            raise failure

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(listener_type(Listener()).wait_closed())
    assert caught.value is failure
    assert calls == (['close'] if failing_method == 'close' else ['close', 'wait'])
    assert phases == ['server_listener_wait_entered']


@pytest.mark.parametrize('scenario', ['joint', 'split-partial', 'empty'])
def test_worker_frames_validate_against_real_component_and_original_unicode(scenario):
    selected = profile(scenario)
    observed = {}
    for component in selected.components:
        original = request(selected, component)
        response = v2.validate_response(selected, original, fixture.worker_response(selected, scenario, original, lambda: False))
        for item in response.items:
            for task in item.tasks:
                observed.setdefault(task.payload.task, []).append(task.payload)
    topics, mentions = observed['message_topics'], observed['concept_mentions']
    if scenario == 'empty':
        assert all(task.outcome == 'assessed_no_match' and task.coverage.complete and not task.findings
                   for task in topics + mentions)
        return
    assert sum(finding.concept_id == 'concept.synthetic' for task in mentions for finding in task.findings) == 4
    assert sum(finding.concept_id == 'concept.secondary' for task in mentions for finding in task.findings) == 1
    assert all(task.coverage.complete for task in mentions)
    rows = fixture.source_fixture()['messages']
    for row, task in zip(rows, mentions):
        for finding in task.findings:
            assert row['text'][finding.span.start:finding.span.end] == (
                'café' if finding.concept_id == 'concept.synthetic' else 'tea')
    assert sum(finding.topic_id == 'topic.synthetic' for task in topics for finding in task.findings) == 2
    assert sum(finding.topic_id == 'topic.secondary' for task in topics for finding in task.findings) == 1
    assert all(task.outcome == ('partial' if scenario == 'split-partial' else 'assessed_findings') for task in topics)
    assert all(task.coverage.complete == (scenario != 'split-partial') for task in topics)
    expected = fixture.source_fixture()['scenarios'][scenario]
    assert expected['mentions']['additional_categories'] == 1
    assert expected['topic']['additional_categories'] == (None if scenario == 'split-partial' else 1)


def test_worker_refuses_unselected_source_and_cancellation():
    selected = profile('joint')
    original = request(selected, selected.components[0])
    with pytest.raises(ValueError):
        fixture.worker_response(selected, 'joint', original, lambda: True)
    item = v2.WorkerInput(request_item_id='foreign', text='other', text_sha256=hashlib.sha256(b'other').hexdigest())
    with pytest.raises(ValueError):
        fixture.worker_response(selected, 'joint', original.model_copy(update={'items': (item,)}), lambda: False)


def test_disposable_root_refuses_existing_unmarked_store_and_foreign_paths(tmp_path):
    configured = configuration(tmp_path)
    configured.auth_database_path.write_bytes(b'preserved')
    with pytest.raises(ValueError):
        fixture.prepare_paths(configured, 'joint')
    assert configured.auth_database_path.read_bytes() == b'preserved'
    configured.canonical_database_path = tmp_path/'foreign.sqlite3'
    with pytest.raises(ValueError):
        fixture.validate_paths(configured, 'joint')
    assert not configured.canonical_database_path.exists()


@pytest.mark.parametrize('scenario', ['saved-only', 'unknown', None])
def test_unadmitted_selector_never_creates_marker(tmp_path, scenario):
    configured = configuration(tmp_path)
    with pytest.raises(ValueError):
        fixture.prepare_paths(configured, scenario)
    assert not tuple(configured.auth_database_path.parent.iterdir())


def test_incomplete_marker_is_private_and_not_restart_authority(tmp_path):
    from app.persistence.private_files import private_file_identity
    configured = configuration(tmp_path)
    paths, old = fixture.prepare_paths(configured, 'joint')
    assert old is None and private_file_identity(paths.root/fixture.MARKER, repair_permissions=False)
    original = (paths.root/fixture.MARKER).read_bytes()
    with pytest.raises(ValueError):
        fixture.prepare_paths(configured, 'joint')
    assert (paths.root/fixture.MARKER).read_bytes() == original


def test_restart_preflight_preserves_exact_marker_and_rejects_changed_scenario(tmp_path):
    configured = configuration(tmp_path)
    paths, _ = fixture.prepare_paths(configured, 'joint')
    for path in (paths.auth, paths.canonical, paths.projection, paths.analytics):
        path.write_bytes(b'retained')
    document = {'schema': 'e2e-text-analysis-store.v1', 'phase': 'ready', 'scenario': 'joint',
        'fixture_sha256': hashlib.sha256(fixture.SOURCE.read_bytes()).hexdigest(),
        'scope': {}, 'principal_id': 'principal', 'account_id': 'account', 'grant_reference_ids': [],
        'auth_store_id': 'auth', 'canonical_store_id': 'canonical', 'profile_sha256': 'a'*64, 'contexts': {}}
    fixture._publish(paths.root/fixture.MARKER, document)
    before = {path: path.read_bytes() for path in paths.root.iterdir()}
    assert fixture.prepare_paths(configured, 'joint') == (paths, document)
    assert fixture.prepare_paths(configured, 'saved-only') == (paths, document)
    with pytest.raises(ValueError):
        fixture.prepare_paths(configured, 'empty')
    assert {path: path.read_bytes() for path in paths.root.iterdir()} == before


def test_frozen_and_non_test_settings_refuse_before_writing(tmp_path, monkeypatch):
    configured = configuration(tmp_path)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    with pytest.raises(ValueError, match='findings_execution_unavailable'):
        fixture.prepare_paths(configured, 'joint')
    monkeypatch.delattr(sys, 'frozen')
    monkeypatch.setattr(settings, 'environment', 'production')
    with pytest.raises(ValueError, match='findings_execution_unavailable'):
        fixture.prepare_paths(configured, 'joint')
    assert not tuple(configured.auth_database_path.parent.iterdir())


@pytest.mark.parametrize('raw', [b'', b'stop', b'stop\r\n', b'start\n', b'stop\nextra', b'stop\nstop\n'])
def test_control_pipe_rejects_everything_except_exact_stop_and_eof(raw):
    assert not fixture.read_stop(BytesIO(raw))
    calls = []
    class Stream(BytesIO):
        def readline(self, size=-1):
            calls.append(('readline', size))
            return super().readline(size)
        def read(self, size=-1):
            calls.append(('read', size))
            return super().read(size)
    def observed():
        calls.append(('seen', None))
        raise RuntimeError('Observer unavailable')
    assert not fixture.read_stop(Stream(raw), command_seen=observed)
    assert calls == ([('readline', 6), ('seen', None), ('read', 1)]
                     if raw.startswith(b'stop\n') else [('readline', 6)])


def test_control_pipe_accepts_exact_stop_and_eof():
    assert fixture.read_stop(BytesIO(b'stop\n'))
    for raises in (False, True):
        calls = []
        class Stream(BytesIO):
            def readline(self, size=-1):
                calls.append(('readline', size))
                return super().readline(size)
            def read(self, size=-1):
                calls.append(('read', size))
                return super().read(size)
        def observed():
            calls.append(('seen', None))
            if raises:
                raise RuntimeError('Observer unavailable')
            return False
        assert fixture.read_stop(Stream(b'stop\n'), command_seen=observed)
        assert calls == [('readline', 6), ('seen', None), ('read', 1)]


def test_signed_identity_setup_leaves_credential_and_session_to_browser(tmp_path, monkeypatch):
    from app.security.webauthn import configured_webauthn_authority_port
    from app.security.runtime_policy import RuntimePolicy
    from app.security.hosted_grants import HostedGrantClient
    verified_inputs = {}
    original_verify = HostedGrantClient._verify_bundle
    def verified_bundle(client, tokens, **context):
        result = original_verify(client, tokens, **context)
        verified_inputs.update({kind: hashlib.sha256(token.encode('ascii')).hexdigest()
                                for kind, token in tokens.items()})
        return result
    monkeypatch.setattr(HostedGrantClient, '_verify_bundle', verified_bundle)
    owner = fixture.FixtureOwner.__new__(fixture.FixtureOwner)
    owner.auth = SQLiteAuthenticationStore(tmp_path/'auth.sqlite3')
    owner.identity_owner = TextAnalysisIdentityOwner(owner.auth)
    cases = json.loads((SOURCE.parents[3]/'contracts/text-analysis-authority-v1/signed-cases.json').read_bytes())
    case = next(row for row in cases if row['case_id'] == 'license-accepted')
    owner.scope = {name: case['payload'][name] for name in
        ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}
    owner.account = fixture.source_fixture()['account_id']
    owner._seed_identity()
    baseline = owner._baseline()
    assert set(baseline.identity_authority.grant_reference_ids) == set(owner.grant_ids)
    grants = owner.auth.verified_grants()
    assert len(grants) == 3
    assert set(verified_inputs) == {'installation_grant', 'membership_snapshot', 'creator_account_binding'}
    assert {grant.reference_id for grant in grants} == set(owner.grant_ids)
    assert {grant.grant_type: grant.grant_digest for grant in grants} == verified_inputs
    for grant in grants:
        if grant.grant_type == 'membership_snapshot':
            assert grant.compact_jws is None
        else:
            assert grant.compact_jws is not None
            assert hashlib.sha256(grant.compact_jws.encode('ascii')).hexdigest() == grant.grant_digest
    assert len(owner.auth.authorized_account_bindings()) == 1
    with owner.auth.database.read() as db:
        assert not db.execute('SELECT 1 FROM webauthn_credentials').fetchone()
        assert not db.execute('SELECT 1 FROM bridge_sessions').fetchone()
    assert baseline.identity.principal_id == owner.principal
    # The real first-registration authority sees the complete stored tuple.
    port = configured_webauthn_authority_port(owner.auth)
    decision = port.registration_authority(RuntimePolicy(identity=None, authorization_epoch=owner.auth.read_epoch()))
    assert decision.authority is not None
    assert decision.authority.creator_account_id == owner.account
    assert decision.authority.principal_id == owner.principal
    assert decision.authority.installation_id == owner.scope['installation_id']
    assert decision.authority.role == 'creator'
    assert set(decision.authority.grant_reference_ids) == set(owner.grant_ids)


def test_real_artifact_installation_does_not_open_engine(tmp_path):
    from app.persistence.nlp_dependencies import DependencyPersistence
    from app.nlp.profiles import artifact_closure_digest
    owner = fixture.FixtureOwner.__new__(fixture.FixtureOwner)
    owner.scenario = 'joint'
    owner.paths = SimpleNamespace(root=tmp_path)
    owner.auth = SQLiteAuthenticationStore(tmp_path/'auth.sqlite3')
    owner._install_data()
    available = owner.artifacts.reopen(owner.profile)
    stored, closure, state = DependencyPersistence(owner.auth).profiles()[0]
    assert stored == owner.profile and state == 'available'
    assert closure == available.data_closure_sha256 == artifact_closure_digest(tuple(
        artifact for artifact in stored.artifacts if artifact.role in ('weights', 'tokenizer', 'configuration')))
    assert not hasattr(owner, 'engine')


def test_fixture_transport_never_opens_network():
    with pytest.raises(RuntimeError, match='text_analysis_fixture_network_unavailable'):
        fixture.NoNetwork().request('POST', '/anything')


def test_source_seed_queues_real_projection_work_without_opening_engine(tmp_path, monkeypatch):
    from threading import Lock
    from app.persistence.factory import create_canonical_repositories

    configured = configuration(tmp_path)
    repositories = create_canonical_repositories('sqlite', canonical_path=configured.canonical_database_path,
                                                 projection_path=configured.projection_database_path)
    owner = fixture.FixtureOwner.__new__(fixture.FixtureOwner)
    owner.canonical, owner.account, owner.scenario = repositories.database, fixture.source_fixture()['account_id'], 'joint'
    owner._lock = Lock()
    owner.prepare_count = owner.analyze_count = 0
    owner.engine = owner._engine()
    def unavailable(*args, **kwargs):
        pytest.fail('Conversation projection opened an Engine')
    monkeypatch.setattr(owner.engine, 'open', unavailable)
    owner._seed_sources()
    with repositories.database.read() as db:
        assert [tuple(row) for row in db.execute(
            'SELECT creator_account_id,canonical_revision,work_kind,conversation_id,completed_at FROM projection_work'
        )] == [(owner.account, 1, 'reseed', None, None)]
        assert not db.execute('SELECT 1 FROM projection_activation_intents').fetchone()
    assert repositories.projection.pending_accounts() == [owner.account]
    assert repositories.projection.state(owner.account)['reason'] == 'projection_missing'
    snapshot = repositories.projection.catch_up(owner.account)
    assert snapshot is not None and snapshot['creator_account_id'] == owner.account
    assert snapshot['view_revision'] > 0 and len(snapshot['conversations']) == 2
    assert snapshot['projection']['status'] == 'current'
    assert snapshot['projection']['canonical_revision'] == snapshot['projection']['projected_revision'] == 1
    activated = repositories.projection.active_generation(owner.account)
    assert activated is not None
    with repositories.database.read() as db:
        assert db.execute('SELECT completed_at FROM projection_work').fetchone()[0] is not None
        assert tuple(db.execute('SELECT state,generation_id,target_canonical_revision FROM projection_activation_intents').fetchone()) == (
            'activated', activated['generation_id'], 1)
        assert not db.execute('SELECT 1 FROM knowledge_jobs').fetchone()
    with repositories.projection.database.read() as db:
        actual = [tuple(row) for row in db.execute(
            'SELECT message_id,text,direction,sentiment FROM projection_messages ORDER BY message_id')]
        expected = sorted((row['message_id'], row['text'], row['direction'], 'unknown')
                          for row in fixture.source_fixture()['messages'])
        assert actual == expected
        assert [tuple(row) for row in db.execute(
            'SELECT analysis_status,sentiment,analyzer_id FROM projection_message_analysis'
        )] == [('unavailable', 'unknown', None)] * 2
    assert owner.prepare_count == owner.analyze_count == owner.engine.prepares == 0
    assert not owner.engine.events
    assert repositories.projection.pending_accounts() == []
    assert repositories.projection.catch_up(owner.account) is None
    reopened = create_canonical_repositories('sqlite', canonical_path=configured.canonical_database_path,
                                             projection_path=configured.projection_database_path)
    assert reopened.projection.active_generation(owner.account) == activated
    assert reopened.projection.state(owner.account) == snapshot['projection']
    assert reopened.projection.catch_up(owner.account) is None
