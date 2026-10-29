"""Durable partial data remains a cache beneath signed artifact verification."""
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.services.nlp_distribution import BLOCK_BYTES, ProfileDistribution
from tests.continuous_analytics_fixture import cleanup
from tests.test_nlp_artifact_store import example, profile_from
from tests.test_nlp_dependencies import Environment

pytestmark = pytest.mark.ci_tier('integration')


@pytest.fixture
def interrupted(tmp_path):
    def profile(kind):
        original, payloads = example(kind)
        document = original.model_dump(mode='json')
        target = next(a for a in document['artifacts'] if a['role'] == 'weights')
        raw = b'invented bytes ' * 14000
        payloads[target['artifact_id']] = raw
        target.update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        document['resources']['temporary_bytes'] = 4 * 1024 * 1024
        return profile_from(document), payloads

    env = Environment(tmp_path, profile_factory=profile)
    asset = next(a for a in env.profile.artifacts if a.role == 'weights')
    relative = 'data/' + asset.sha256
    raw = env.objects[relative]
    state = SimpleNamespace(mode='range', requests=[], first=True)

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield raw[:BLOCK_BYTES]
            raise httpx.ReadError('interrupted')

    def respond(request):
        path = request.url.path.removeprefix('/nlp/')
        if path != relative:
            return httpx.Response(200, stream=httpx.ByteStream(env.objects[path]))
        state.requests.append(request.headers.get('range'))
        if state.first:
            state.first = False
            return httpx.Response(200, stream=Interrupted())
        if state.mode == 'ignore':
            return httpx.Response(200, stream=httpx.ByteStream(raw))
        assert request.headers['range'] == f'bytes={BLOCK_BYTES}-'
        total = len(raw) + (1 if state.mode == 'wrong_total' else 0)
        remaining = raw[BLOCK_BYTES:]
        if state.mode == 'bad_hash':
            remaining = b'!' * len(remaining)
        return httpx.Response(206, headers={'Content-Range': f'bytes {BLOCK_BYTES}-{len(raw)-1}/{total}'},
                              stream=httpx.ByteStream(remaining))

    env.distribution = ProfileDistribution('https://example.invalid/nlp/', transport=httpx.MockTransport(respond))
    env.service.distribution = env.distribution
    try:
        operation_id = env.acquire()
        op = env.service._operation(operation_id)
        assert op['state'] == 'interrupted' and not env.service.ready(env.profile)
        partial = op['partials'][asset.artifact_id]
        assert partial['bytes'] == BLOCK_BYTES
        assert partial['sha256'] == hashlib.sha256(raw[:BLOCK_BYTES]).hexdigest()
        name = hashlib.sha256(asset.artifact_id.encode()).hexdigest() + '.part'
        path = env.service.spool / operation_id / name
        assert path.read_bytes() == raw[:BLOCK_BYTES]
        yield env, state, operation_id, path, raw
    finally:
        assert env.service.close()
        cleanup(env.fixture)


def resume(env, operation_id):
    env.service.resume(operation_id)
    env.service.thread.join(10)
    assert not env.service.thread.is_alive()
    return env.service._operation(operation_id)


@pytest.mark.parametrize('mode', ['range', 'ignore'])
def test_reopen_rehashes_prefix_and_handles_range_or_full_restart(interrupted, mode):
    env, state, operation_id, path, raw = interrupted
    assert env.service.close()
    env.owners.birth = 'two'
    env.service = env.construct()
    env.service.start()
    assert state.requests == [None]
    state.mode = mode
    # A tail not included in the durable checkpoint is never reused.
    with path.open('ab') as stream:
        stream.write(b'uncommitted')
    assert resume(env, operation_id)['state'] == 'complete'
    assert state.requests == [None, f'bytes={BLOCK_BYTES}-']
    available = env.artifacts.reopen(env.profile)
    assert any(a.path.read_bytes() == raw for a in available.artifacts)
    assert not path.exists()


@pytest.mark.parametrize('mutation', ['prefix', 'binding', 'length'])
def test_changed_prefix_or_manifest_binding_is_refused_before_request(interrupted, mutation):
    env, state, operation_id, path, raw = interrupted
    if mutation == 'prefix':
        with path.open('r+b') as stream:
            stream.write(b'!')
    elif mutation == 'length':
        with path.open('r+b') as stream:
            stream.truncate(1)
    else:
        op = env.service._operation(operation_id)
        next(iter(op['partials'].values()))['artifact_sha256'] = '0' * 64
        env.persistence.operation(op)
    assert resume(env, operation_id)['state'] == 'interrupted'
    assert state.requests == [None]
    assert not env.service.ready(env.profile)


@pytest.mark.parametrize('mode', ['wrong_total', 'bad_hash'])
def test_invalid_range_or_final_digest_cannot_publish(interrupted, mode):
    env, state, operation_id, path, raw = interrupted
    state.mode = mode
    op = resume(env, operation_id)
    assert op['state'] == 'interrupted'
    assert not env.service.ready(env.profile)
    assert not env.artifacts.root.exists() or not list(env.artifacts.root.iterdir())
    if mode == 'bad_hash':
        assert path.stat().st_size == 0
        assert next(iter(op['partials'].values()))['bytes'] == 0


def test_cancel_discards_only_owned_partial_cache(interrupted):
    env, _, operation_id, path, _ = interrupted
    env.service.pause(operation_id, cancel=True)
    op = env.service._operation(operation_id)
    assert op['state'] == 'cancelled' and op['partials'] == {}
    assert not path.parent.exists()


def test_restart_journal_survives_interruption_before_old_prefix_truncation(interrupted, monkeypatch):
    env, state, operation_id, path, _ = interrupted
    state.mode = 'ignore'
    original = env.persistence.operation
    interrupted_once = False

    def save(value):
        nonlocal interrupted_once
        original(value)
        partials = value.get('partials', {})
        if (not interrupted_once and value['id'] == operation_id and value['state'] == 'running'
                and partials and next(iter(partials.values()))['bytes'] == 0):
            interrupted_once = True
            raise OSError('interrupted after zero checkpoint')

    monkeypatch.setattr(env.persistence, 'operation', save)
    op = resume(env, operation_id)
    assert interrupted_once and op['state'] == 'interrupted'
    assert next(iter(op['partials'].values()))['bytes'] == 0
    assert path.stat().st_size == BLOCK_BYTES
    monkeypatch.setattr(env.persistence, 'operation', original)
    assert resume(env, operation_id)['state'] == 'complete'
    assert state.requests == [None, f'bytes={BLOCK_BYTES}-', None]


def test_stream_only_transport_can_restart_without_appending(interrupted):
    env, state, operation_id, _, _ = interrupted
    assert env.service.close()
    env.owners.birth = 'two'
    env.distribution = SimpleNamespace(stream=env.distribution.stream)
    env.service = env.construct()
    env.service.start()
    state.mode = 'ignore'
    assert env.service.resume_granularity == 'verified_files_current_file_restarts'
    assert resume(env, operation_id)['state'] == 'complete'
    assert state.requests == [None, None]
