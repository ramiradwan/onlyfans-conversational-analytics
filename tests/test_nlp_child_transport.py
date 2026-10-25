"""Invented private frames, retained data and owned-session failure cases."""
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import hashlib
from io import BytesIO
import json
import os
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from app.nlp import adapter, windows_host
from app.nlp.child_client import ChildClient
from app.nlp.child_protocol import (BootstrapResult, CloseResult, HostControl, HostStatus, OwnedLaunchError,
    decode_envelope, encode_envelope, payload_bytes, read_frame, write_frame)
from app.nlp.engine_provider import ProcessEngineProvider
from app.nlp.profiles import BASELINE_RESOURCE_POLICY
from app.nlp.retained_artifacts import retain_data
from app.nlp.v2 import _decode, WorkerRequest, WorkerInput, WorkerResponse
from tests.test_nlp_artifact_store import example, store, streams
from tests.test_nlp_processing_publication import ConformanceAdapter

pytestmark = pytest.mark.ci_tier('fast')
NONCE = 'a' * 32


@pytest.mark.parametrize('failure', ['status', 'cancelled', 'deadline'])
def test_failed_observation_retains_exact_drained_owner_after_launcher_removal(monkeypatch, failure):
    profile, _ = example()
    identity = profile.components[0].engine
    launcher = windows_host.NativeHostLauncher.__new__(windows_host.NativeHostLauncher)
    launcher._lock, launcher._owners = threading.Lock(), set()
    launcher._observed, launcher._stopping = identity, False
    launcher.monotonic = time.monotonic
    owners, closed = [], []

    def spawn(nonce, artifacts, check):
        check()
        status = BytesIO()
        write_frame(status, payload_bytes(HostStatus(schema='nlp-host-status.v1', channel_nonce=nonce, state='failed',
            reason='identity_mismatch', worker_pid=None, tree_drained=True, failure_stage='parent_access')))
        status.seek(0)
        resources = ExitStack()
        resources.callback(lambda: closed.append(True))
        owner = windows_host._HostOwner(nonce, resources, BytesIO(), status,
            BytesIO(), BytesIO(), BytesIO(), (), time.monotonic, launcher._finished)
        owner.process = SimpleNamespace(returncode=1, wait=lambda **kwargs: 1)
        launcher._owners.add(owner)
        owners.append(owner)
        owner.start()
        owner._reader.join(1)
        assert not owner._reader.is_alive()
        return owner

    calls = []
    def check():
        calls.append(True)
        if len(calls) > 1 and failure == 'cancelled':
            raise RuntimeError('invented cancellation detail')

    monkeypatch.setattr(launcher, '_spawn', spawn)
    with pytest.raises(OwnedLaunchError) as raised:
        launcher.observe(identity, timeout=0 if failure == 'deadline' else 1, check=check)
    owner = raised.value.owner
    assert owner is owners[0] and owner._closed and closed == [True]
    assert owner._terminal.state == 'failed' and owner._terminal.reason == 'identity_mismatch'
    assert owner._terminal.failure_stage == 'parent_access'
    assert owner._terminal.tree_drained and not owner._failure and not owner.graceful
    assert not owner._reader.is_alive() and all(not thread.is_alive() for thread in owner._writers)
    assert launcher._owners == set() and not launcher.current(identity)
    assert str(raised.value) == 'nlp_host_launch_failed' and raised.value.__cause__ is None
    assert 'invented cancellation detail' not in str(raised.value)
    assert owner.abort(0) and launcher.close(0)


def capabilities(profile):
    return tuple(adapter.AdapterCapability(adapter=c.adapter,
        task_contracts=tuple(t.task_contract for t in c.tasks),
        score_kinds=tuple(dict.fromkeys(t.score_policy.kind for t in c.tasks)),
        max_source_codepoints=c.max_source_codepoints, max_batch_items=c.max_batch_items,
        max_windows=c.max_windows, max_candidates=c.max_candidates,
        max_output_findings=c.max_output_findings,
        runtime_requirements_sha256=c.runtime_requirements.sha256) for c in profile.components)


class ChannelOwner:
    def __init__(self, profile, *, tail=b'', delay=0, bad_bootstrap=False, cancellable=False,
                 cancellable_prepare=False):
        read, write = os.pipe()
        self.input = os.fdopen(read, 'rb', buffering=0)
        self.stdin = os.fdopen(write, 'wb', buffering=0)
        read, write = os.pipe()
        self.stdout = os.fdopen(read, 'rb', buffering=0)
        self.output = os.fdopen(write, 'wb', buffering=0)
        self.profile, self.tail, self.delay = profile, tail, delay
        self.bad_bootstrap, self.cancellable = bad_bootstrap, cancellable
        self.cancellable_prepare = cancellable_prepare
        self.allow_drain, self.graceful = True, True
        self.operations = []
        self.replies = []
        self.resident_health = 'unloaded'
        self.cancel_replied = threading.Event()
        self.entered = threading.Event()
        self.worker = threading.Thread(target=self._serve, daemon=False)
        self.worker.start()

    def reply(self, operation, payload):
        self.replies.append((operation, payload))
        write_frame(self.output, encode_envelope(operation, payload))

    def _serve(self):
        active = None
        try:
            while True:
                operation, body = decode_envelope(read_frame(self.input))
                request = json.loads(body)
                self.operations.append(operation)
                if operation == 'bootstrap':
                    self.reply(operation, BootstrapResult(channel_nonce=request['channel_nonce'],
                        profile_sha256='f' * 64 if self.bad_bootstrap else self.profile.identity_digest(),
                        outcome='bound', reason='none'))
                elif operation == 'handshake':
                    self.reply(operation, adapter.WorkerHandshake(request_id=request['request_id'],
                        engine=self.profile.components[0].engine, adapters=capabilities(self.profile)))
                elif operation == 'prepare':
                    if self.cancellable_prepare:
                        active = adapter.PrepareRequest.model_validate_json(body)
                        self.entered.set()
                    else:
                        self.resident_health = 'ready'
                        self.reply(operation, adapter.PreparationResult(**{k: request[k] for k in
                            ('request_id', 'profile_sha256', 'component_id', 'component_sha256')},
                            outcome='ready', reason='none'))
                elif operation == 'analyze':
                    active = WorkerRequest.model_validate_json(body)
                    self.resident_health = 'ready'
                    self.entered.set()
                    if not self.cancellable:
                        self.reply(operation, _decode(WorkerResponse,
                            ConformanceAdapter(self.profile)(active, lambda: False), 'response'))
                        active = None
                elif operation in ('cancel', 'unload'):
                    health = self.resident_health if operation == 'cancel' else 'unloaded'
                    if operation == 'cancel' and active is not None:
                        if isinstance(active, WorkerRequest):
                            health = 'ready'
                            self.reply('analyze', _decode(WorkerResponse,
                                ConformanceAdapter(self.profile)(active, lambda: False), 'response'))
                        else:
                            health = 'unloaded'
                            self.reply('prepare', adapter.PreparationResult(**{k: getattr(active, k) for k in
                                ('request_id', 'profile_sha256', 'component_id', 'component_sha256')},
                                outcome='cancelled', reason='cancelled'))
                        active = None
                    self.resident_health = health
                    self.reply(operation, adapter.LifecycleResult(operation=operation,
                        request_id=request['request_id'], profile_sha256=request['profile_sha256'],
                        component_sha256=request['component_sha256'],
                        target_request_id=request.get('target_request_id'),
                        outcome='completed', health=health))
                    if operation == 'cancel':
                        self.cancel_replied.set()
                elif operation == 'close':
                    time.sleep(self.delay)
                    self.reply(operation, CloseResult(request_id=request['request_id'], outcome='drained', reason='none'))
                    if self.tail:
                        self.output.write(self.tail)
                    return
                else:
                    return
        except (ValueError, EOFError, OSError):
            pass
        finally:
            self.input.close()
            self.output.close()

    def close(self, timeout):
        self.worker.join(timeout)
        if not self.worker.is_alive() and self.allow_drain:
            self.stdin.close()
            return True
        return False

    def abort(self, timeout):
        self.graceful = False
        self.stdin.close()
        self.worker.join(timeout)
        return not self.worker.is_alive() and self.allow_drain


@pytest.mark.parametrize('header', [b'', b'\x01', b'\x02\x00\x00\x00x', struct.pack('<I', 0),
    struct.pack('<I', 1_048_577)])
def test_frame_refuses_clean_eof_truncation_and_invalid_length(header):
    with pytest.raises((ValueError, EOFError)):
        read_frame(BytesIO(header))


@pytest.mark.parametrize('body', [b'{"channel":"nlp-child.v1","operation":"close","payload":{"x":1,"x":2}}',
    b'{"channel":"nlp-child.v1","operation":"unknown","payload":{}}',
    b'{"channel":"nlp-child.v1","operation":"close","payload":{},"extra":1}',
    b'\xff', b'[]'])
def test_envelope_rejects_malformed_and_ambiguous_dispatch(body):
    with pytest.raises(ValueError):
        decode_envelope(body)


def test_host_status_separates_physical_drain_from_failure():
    body = dict(schema='nlp-host-status.v1', channel_nonce=NONCE, state='failed',
        reason='worker_failure', worker_pid=123, tree_drained=True)
    assert _decode(HostStatus, json.dumps(body).encode(), 'host').tree_drained
    body.update(state='running', reason='none')
    with pytest.raises(ValueError):
        _decode(HostStatus, json.dumps(body).encode(), 'host')
    assert json.loads(payload_bytes(HostControl(channel_nonce=NONCE, operation='close')))['schema'] == 'nlp-host-control.v1'


@pytest.mark.parametrize('stage', ['parent_access', 'descriptor_validation', 'engine_selection', None])
def test_failed_host_stage_remains_closed_and_optional(stage):
    body = dict(schema='nlp-host-status.v1', channel_nonce=NONCE, state='failed',
        reason='identity_mismatch', worker_pid=None, tree_drained=True)
    assert _decode(HostStatus, json.dumps(body).encode(), 'host').failure_stage is None
    body['failure_stage'] = stage
    assert _decode(HostStatus, json.dumps(body).encode(), 'host').failure_stage == stage


@pytest.mark.parametrize('mutation', [dict(failure_stage='exception detail'), dict(failure_stage=1),
    dict(failure_stage={'path': 'untrusted'}), dict(state='ready', reason='none'),
    dict(state='drained', reason='none'),
    dict(state='running', reason='none', worker_pid=123, tree_drained=False)])
def test_host_stage_rejects_unknown_data_and_nonfailed_states(mutation):
    body = dict(schema='nlp-host-status.v1', channel_nonce=NONCE, state='failed',
        reason='identity_mismatch', worker_pid=None, tree_drained=True, failure_stage='parent_access')
    body.update(mutation)
    with pytest.raises(ValueError):
        _decode(HostStatus, json.dumps(body).encode(), 'host')


@pytest.mark.parametrize('tail', [b'\x01', struct.pack('<I', 2) + b'{}'])
def test_close_rejects_post_terminal_truncated_or_extra_frames(tail):
    profile, _ = example()
    owner = ChannelOwner(profile, tail=tail)
    client = ChildClient(profile, owner, NONCE)
    assert client.close(2)
    assert not client.graceful
    owner.stdout.close()


def test_delayed_close_is_graceful_and_concurrent_close_is_budgeted():
    profile, _ = example()
    owner = ChannelOwner(profile, delay=.08)
    client = ChildClient(profile, owner, NONCE)
    result = []
    closing = threading.Thread(target=lambda: result.append(client.close(2)))
    closing.start()
    while not client._closing:
        time.sleep(.001)
    before = time.monotonic()
    assert client.close(0) is False
    assert time.monotonic() - before < .1
    closing.join(2)
    assert result == [True] and client.graceful
    owner.stdout.close()


def test_cancel_ack_does_not_publish_completed_analysis():
    profile, _ = example()
    owner = ChannelOwner(profile, cancellable=True)
    client = ChildClient(profile, owner, NONCE)
    try:
        component = profile.components[0]
        text = 'A scarf and a printed brochure.'
        request = WorkerRequest(request_id='request', profile_sha256=profile.identity_digest(),
            component_id=component.component_id, component_sha256=component.identity_digest(),
            tasks=tuple(t.task for t in component.tasks), deadline_ms=1000,
            items=(WorkerInput(request_item_id='message', text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest()),))
        with pytest.raises(ValueError, match='nlp_cancelled'):
            client.analyze(request, lambda: owner.entered.is_set())
        assert owner.cancel_replied.wait(1)
        assert [operation for operation, _ in owner.replies] == ['bootstrap', 'analyze', 'cancel']
        terminal, ack = owner.replies[-2:]
        assert terminal[1].request_id == request.request_id
        assert ack[1].target_request_id == request.request_id and ack[1].health == 'ready'
        with client._condition:
            assert client._condition.wait_for(lambda: not client._pending or client._failed, timeout=1)
        owner.cancellable = False
        recovered = _decode(WorkerResponse, client.analyze(request.model_copy(update={'request_id': 'after-cancel'}), lambda: False), 'response')
        assert recovered.request_id == 'after-cancel'
        late_cancel = adapter.CancelRequest(request_id='late-cancel', target_request_id='after-cancel',
            profile_sha256=request.profile_sha256, component_sha256=request.component_sha256)
        pending = client._submit('cancel', late_cancel,
            lambda body: adapter.validate_lifecycle(late_cancel, body))
        assert pending.event.wait(1) and not pending.failed
        assert adapter.validate_lifecycle(late_cancel, pending.body).health == 'ready'
        assert client.healthy
        assert client.close(2)
    finally:
        client.close(2)
        owner.stdout.close()


def test_cancellation_during_preparation_settles_before_reprepare():
    profile, _ = example()
    owner = ChannelOwner(profile, cancellable_prepare=True)
    client = ChildClient(profile, owner, NONCE)
    try:
        component = profile.components[0]
        request = adapter.PrepareRequest(request_id='prepare-request', profile_sha256=profile.identity_digest(),
            component_id=component.component_id, component_sha256=component.identity_digest(),
            artifacts=tuple(a for a in profile.artifacts if a.artifact_id in component.artifact_ids
                or a.role in ('engine', 'runtime')), max_memory_bytes=profile.resources.transition_peak_memory_bytes,
            inference_threads=profile.resources.inference_threads)
        with pytest.raises(ValueError, match='nlp_cancelled'):
            client.prepare(request, cancelled=lambda: owner.entered.is_set())
        assert owner.cancel_replied.wait(1)
        assert owner.operations == ['bootstrap', 'prepare', 'cancel']
        assert [operation for operation, _ in owner.replies] == ['bootstrap', 'prepare', 'cancel']
        terminal, ack = owner.replies[-2:]
        assert terminal[1].request_id == request.request_id
        assert terminal[1].outcome == terminal[1].reason == 'cancelled'
        assert ack[1].target_request_id == request.request_id and ack[1].health == 'unloaded'
        with client._condition:
            assert client._condition.wait_for(lambda: not client._pending or client._failed, timeout=1)
        owner.cancellable_prepare = False
        recovered = _decode(adapter.PreparationResult,
            client.prepare(request.model_copy(update={'request_id': 'prepare-recovery'})), 'prepare')
        assert recovered.request_id == 'prepare-recovery' and recovered.outcome == 'ready'
        assert client.healthy
        assert client.close(2)
    finally:
        client.close(2)
        owner.stdout.close()


def test_retained_data_rechecks_members_and_holds_readonly_bytes(tmp_path):
    profile, payloads = example()
    available = store(tmp_path).stage(profile, streams(payloads))
    with retain_data(profile, available, lambda: None) as retained:
        assert {a.artifact.artifact_id for a in retained.artifacts} == set(payloads)
        assert [s.read() for s in retained.streams] == [payloads[a.artifact.artifact_id] for a in retained.artifacts]
        if os.name == 'nt':
            with pytest.raises(OSError):
                available.artifacts[0].path.write_bytes(b'changed')
    assert all(s.closed for s in retained.streams)
    with pytest.raises(ValueError):
        with retain_data(profile, replace(available, profile_sha256='0' * 64), lambda: None):
            pass


class Launcher:
    def __init__(self, profile, *, bad=False, drained=True):
        self.profile, self.bad, self.drained_value, self.owners = profile, bad, drained, []

    def current(self, identity):
        return identity == self.profile.components[0].engine

    def open(self, profile, files, nonce, check, *, deadline):
        check()
        owner = ChannelOwner(profile, bad_bootstrap=self.bad)
        owner.allow_drain = self.drained_value
        self.owners.append(owner)
        return owner

    def close(self, timeout):
        return all(not owner.worker.is_alive() and owner.allow_drain for owner in self.owners)


def test_provider_keeps_failed_bootstrap_owner_and_files_until_verified_join(tmp_path):
    profile, payloads = example()
    available = store(tmp_path).stage(profile, streams(payloads))
    launcher = Launcher(profile, bad=True, drained=False)
    provider = ProcessEngineProvider(launcher, resource_policy=BASELINE_RESOURCE_POLICY)
    with pytest.raises(ValueError):
        with provider.open(profile, available, lambda: None):
            pass
    assert not provider.drained(profile)
    assert provider.close(0) is False
    for owner in launcher.owners:
        owner.allow_drain = True
    assert provider.close(2)
    assert provider.drained(profile)
    for owner in launcher.owners:
        owner.stdout.close()


def test_provider_serializes_session_residency_and_releases_after_close(tmp_path):
    profile, payloads = example()
    available = store(tmp_path).stage(profile, streams(payloads))
    launcher = Launcher(profile)
    provider = ProcessEngineProvider(launcher, resource_policy=BASELINE_RESOURCE_POLICY)
    second, errors = threading.Event(), []

    def enter():
        try:
            with provider.open(profile, available, lambda: None):
                second.set()
        except BaseException as error:
            errors.append(type(error).__name__)

    with provider.open(profile, available, lambda: None):
        thread = threading.Thread(target=enter)
        thread.start()
        time.sleep(.06)
        assert len(launcher.owners) == 1 and not second.is_set()
    thread.join(3)
    assert not thread.is_alive() and second.is_set() and not errors
    assert provider.drained(profile) and provider.close(2)
    for owner in launcher.owners:
        owner.stdout.close()
