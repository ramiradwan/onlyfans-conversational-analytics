"""Invented data through subprocess interruption; no installed-package qualification."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.nlp.artifact_store import ArtifactStore, StoreLimits
from app.nlp.profiles import ResourcePolicy, canonical_bytes
from app.nlp.release_catalog import CATALOG_TYPE, canonical
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.nlp_dependencies import DependencyPersistence
from app.services.nlp_dependencies import DependencyService, ProcessOwners
from app.services.nlp_distribution import ProfileDistribution
from app.services import nlp_dependencies
from tests.test_nlp_artifact_store import example, profile_from
from tests.test_nlp_dependencies import Engine
from tests.test_nlp_profile_authentication import keys, sign

pytestmark = pytest.mark.ci_tier('integration')
ROOT = Path(__file__).resolve().parents[1]


def _profile():
    profile, payloads = example()
    data = [a for a in profile.artifacts if a.artifact_id in payloads]
    interrupted = data[1].artifact_id
    payloads[interrupted] = b'example-stream-block-' * 8192
    document = profile.model_dump(mode='json')
    document['resources']['temporary_bytes'] = 4 * 1024 * 1024
    asset = next(a for a in document['artifacts'] if a['artifact_id'] == interrupted)
    asset.update(size_bytes=len(payloads[interrupted]),
                 sha256=hashlib.sha256(payloads[interrupted]).hexdigest())
    return profile_from(document), payloads, data[0].artifact_id, interrupted


class DownloadEnvironment:
    def __init__(self, root, *, online=False, interrupt=False):
        self.root, self.online, self.interrupt = root, online, interrupt
        self.profile, self.payloads, self.completed, self.partial = _profile()
        self.requests = []
        self.ranges = []
        # Acquisition requires an available Engine. This protocol fixture does not
        # install or load an executable; the test covers model-data recovery only.
        self.engine = Engine()
        self.persistence = DependencyPersistence(SQLiteAuthenticationStore(root / 'auth.sqlite3'))
        self.artifacts = ArtifactStore(root / 'artifacts', StoreLimits(1024 * 1024, 4 * 1024 * 1024, 0))
        self.objects = {'profiles/' + self.profile.identity_digest(): sign(canonical_bytes(self.profile))}
        self.objects.update({'data/' + hashlib.sha256(raw).hexdigest(): raw for raw in self.payloads.values()})
        self.distribution = ProfileDistribution('https://example.invalid/nlp/',
                                                transport=httpx.MockTransport(self.respond))
        self.service = DependencyService(self.persistence, self.artifacts,
            SimpleNamespace(selected_profiles=lambda: []), object(), self.distribution,
            keys=keys(), approvals=lambda p: p.identity_digest() == self.profile.identity_digest(),
            engine_provider=self.engine,
            resource_policy=ResourcePolicy(download_bytes=1024 * 1024, installed_bytes=4 * 1024 * 1024,
                temporary_bytes=4 * 1024 * 1024, transition_peak_memory_bytes=1024 * 1024, inference_threads=2),
            spool_root=root / 'spool', clock=lambda: 1000)

    def catalog(self):
        p = self.profile
        return sign(canonical({'schema_version': 'nlp-release-catalog.v1', 'sequence': 1,
            'issued_at': 900, 'expires_at': 2000, 'withdrawals': [], 'profiles': [{
                'profile_id': p.profile_id, 'revision': p.revision, 'profile_sha256': p.identity_digest(),
                'compatibility': {'profile_sha256': p.identity_digest(),
                    'compatible_engines': [p.components[0].engine.model_dump(mode='json')],
                    'adapters': [c.adapter.model_dump(mode='json') for c in p.components]}}]}), typ=CATALOG_TYPE)

    def respond(self, request):
        assert request.url.host == 'example.invalid'
        assert 'if-range' not in request.headers
        relative = request.url.path.removeprefix('/nlp/')
        self.requests.append(relative)
        assert self.online, 'offline reconstruction attempted a request'
        raw = self.objects[relative]
        if 'range' in request.headers:
            offset = int(request.headers['range'].removeprefix('bytes=').removesuffix('-'))
            assert relative.startswith('data/') and 0 < offset < len(raw)
            self.ranges.append((relative, offset))
            return httpx.Response(206, headers={'Content-Range': f'bytes {offset}-{len(raw)-1}/{len(raw)}'},
                                  stream=httpx.ByteStream(raw[offset:]))
        partial_path = 'data/' + hashlib.sha256(self.payloads[self.partial]).hexdigest()
        if self.interrupt and relative == partial_path:
            owner = self

            class InterruptedStream(httpx.SyncByteStream):
                def __iter__(self):
                    yield raw[:65536]
                    operation = next(o for o in owner.persistence.operations() if o['kind'] == 'acquire')
                    assert operation['state'] == 'running' and operation['in_flight']
                    assert set(operation['verified']) == {owner.completed}
                    name = hashlib.sha256(owner.partial.encode()).hexdigest() + '.part'
                    path = owner.service.spool / operation['id'] / name
                    assert path.read_bytes() == raw[:65536]
                    assert operation['partials'][owner.partial]['bytes'] == 65536
                    assert operation['partials'][owner.partial]['sha256'] == hashlib.sha256(raw[:65536]).hexdigest()
                    with path.open('r+b') as stream:
                        os.fsync(stream.fileno())
                    marker = owner.root / 'interrupted.json'
                    pending = marker.with_suffix('.pending')
                    with pending.open('x') as stream:
                        json.dump({'operation': operation, 'requests': owner.requests,
                                   'pid': os.getpid(), 'root': str(ROOT),
                                   'service_source': nlp_dependencies.__file__}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    pending.replace(marker)
                    # The parent kills this exact process after observing durable progress.
                    if not threading.Event().wait(30):
                        raise TimeoutError('interruption was not observed')

            return httpx.Response(200, stream=InterruptedStream())
        return httpx.Response(200, stream=httpx.ByteStream(raw))


def _download_child(root):
    root = Path(root)
    environment = DownloadEnvironment(root, online=True, interrupt=True)
    assert not environment.artifacts.root.exists()
    assert not environment.persistence.profiles()
    assert not environment.service.available
    environment.service.start()
    environment.service.learn(environment.catalog())
    try:
        quote = environment.service.plan(environment.profile.profile_id, environment.profile.revision)
        environment.service.acquire(environment.profile.profile_id, environment.profile.revision,
                                    plan_id=quote['operation_id'])
        environment.service.thread.join(35)
        raise AssertionError('child reached terminal without the expected process interruption')
    finally:
        assert environment.service.close()


def test_first_download_recovers_after_owned_process_death_without_automatic_requests(tmp_path):
    marker = tmp_path / 'interrupted.json'
    environment = None
    # Avoid the Windows virtual-environment redirector creating an unowned child.
    executable = sys._base_executable if os.name == 'nt' else sys.executable
    command = [executable, '-B', '-c',
        'import sys; sys.path.insert(0, sys.argv[1]); '
        'from tests.test_nlp_first_download_recovery import _download_child; _download_child(sys.argv[2])',
        str(ROOT), str(tmp_path)]
    child_environment = os.environ.copy()
    child_environment['PYTHONPATH'] = os.pathsep.join([str(ROOT), *(
        path for path in sys.path if Path(path).name == 'site-packages')])
    # Import-time default stores are also isolated from the checkout and parent.
    defaults = tmp_path / 'defaults'
    defaults.mkdir()
    for key in ('AUTH_DATABASE_PATH', 'CANONICAL_DATABASE_PATH', 'PROJECTION_DATABASE_PATH',
                'ANALYTICS_PROJECTION_DATABASE_PATH'):
        child_environment[key] = str(defaults / (key.lower() + '.sqlite3'))
    child = subprocess.Popen(command, cwd=ROOT, env=child_environment,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 25
        while not marker.exists():
            if child.poll() is not None:
                stdout, stderr = child.communicate(timeout=5)
                pytest.fail((stdout + stderr).decode(errors='replace'))
            assert time.monotonic() < deadline, 'child did not reach durable progress'
            time.sleep(.01)
        # Wait for the complete, fsynced marker rather than interrupting its writer.
        while True:
            try:
                saved = json.loads(marker.read_bytes())
                break
            except json.JSONDecodeError:
                assert time.monotonic() < deadline
                time.sleep(.01)
        assert saved['pid'] == child.pid and saved['root'] == str(ROOT)
        assert Path(saved['service_source']) == ROOT / 'app/services/nlp_dependencies.py'
        owner = saved['operation']['owner']
        assert owner['pid'] == child.pid and not ProcessOwners().absent(owner)
        child.kill()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode is not None and child.returncode != 0, (stdout, stderr)
        # Keep the process handle retained: recovery must observe death immediately,
        # even while Windows still allows queries about this process object.
        assert ProcessOwners().absent(owner)

        environment = DownloadEnvironment(tmp_path)
        service, profile = environment.service, environment.profile
        operation_id = saved['operation']['id']
        assert service._operation(operation_id)['state'] == 'running'
        service.start()
        status = service.status()
        row = next(p for p in status['profiles'] if p['profile_sha256'] == profile.identity_digest())
        assert service.observation(operation_id)['state'] == 'paused'
        assert not row['installed'] and not row['prepared'] and row['storage_state'] == 'planned'
        assert not status['available_profiles'] and not service.ready(profile)
        assert not list(environment.artifacts.root.iterdir())
        assert not environment.requests and environment.engine.prepares == 0
        assert service.thread is None

        environment.online = True
        service.resume(operation_id)
        service.thread.join(10)
        assert not service.thread.is_alive()
        assert service.observation(operation_id)['state'] == 'complete'
        assert service.ready(profile)
        completed = 'data/' + hashlib.sha256(environment.payloads[environment.completed]).hexdigest()
        partial = 'data/' + hashlib.sha256(environment.payloads[environment.partial]).hexdigest()
        assert saved['requests'].count(completed) == 1 and completed not in environment.requests
        assert saved['requests'].count(partial) == 1 and environment.requests.count(partial) == 1
        assert environment.ranges == [(partial, 65536)]
        assert all(path.startswith('data/') for path in environment.requests)
        available = environment.artifacts.reopen(profile)
        for member in available.artifacts:
            expected = environment.payloads[member.artifact.artifact_id]
            assert member.path.read_bytes() == expected
            assert hashlib.sha256(member.path.read_bytes()).hexdigest() == member.artifact.sha256
        assert not (service.spool / operation_id).exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
        assert child.returncode is not None
        if os.name == 'nt':
            child._handle.Close()
        if environment is not None:
            assert environment.service.close()
