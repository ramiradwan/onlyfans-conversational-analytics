"""Cancellation, deadlines and block bounds for streamed data."""
from types import SimpleNamespace

import httpx
import pytest

from app.nlp.release_catalog import DependencyRejected
from app.services.nlp_distribution import BLOCK_BYTES, ProfileDistribution

pytestmark = pytest.mark.ci_tier('fast')


class Incoming(httpx.SyncByteStream):
    def __init__(self, parts, before_read=lambda _: None):
        self.parts, self.before_read = parts, before_read
        self.reads, self.closed = 0, False

    def __iter__(self):
        for part in self.parts:
            self.reads += 1
            self.before_read(self.reads)
            yield part

    def close(self):
        self.closed = True


def distribution(stream, clock=lambda: 0):
    return ProfileDistribution('https://example.invalid/nlp/', clock=clock,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))


@pytest.mark.parametrize('stop', ['deadline', 'cancelled'])
def test_small_network_blocks_observe_stop_before_reading_remaining_body(stop):
    state = SimpleNamespace(now=0, cancelled=False)

    def before_read(count):
        state.now += 1
        state.cancelled = state.cancelled or (stop == 'cancelled' and count == 2)

    def check():
        if state.cancelled:
            raise DependencyRejected('nlp_cancelled')

    source = Incoming((b'a', b'b', b'c', b'd'), before_read)
    target = distribution(source, clock=lambda: state.now)
    deadline = 2 if stop == 'deadline' else 10
    code = 'nlp_acquisition_deadline' if stop == 'deadline' else 'nlp_cancelled'
    with pytest.raises(DependencyRejected, match=code):
        target.read('catalog.jws', 100, check=check, deadline=deadline)
    assert source.reads == 2
    assert source.closed


def test_consumer_cancellation_prevents_another_network_read():
    state = SimpleNamespace(cancelled=False)

    def check():
        if state.cancelled:
            raise DependencyRejected('nlp_cancelled')

    source = Incoming((b'a', b'b', b'c'))
    with distribution(source).stream('catalog.jws', check=check, deadline=10) as blocks:
        assert next(blocks) == b'a'
        state.cancelled = True
        with pytest.raises(DependencyRejected, match='nlp_cancelled'):
            next(blocks)
    assert source.reads == 1
    assert source.closed


def test_large_network_blocks_keep_content_and_bounded_output():
    parts = (b'first', b'x' * (BLOCK_BYTES * 2 + 17), b'last')
    source = Incoming(parts)
    with distribution(source).stream('data/' + 'a' * 64,
                                      check=lambda: None, deadline=10) as blocks:
        received = list(blocks)
    assert b''.join(received) == b''.join(parts)
    assert all(0 < len(block) <= BLOCK_BYTES for block in received)
    assert source.reads == len(parts)
    assert source.closed


def test_artifact_range_uses_safe_large_offsets_and_closes_response():
    offset = 2**32 + 123
    source = Incoming((b'end',))

    def respond(request):
        assert request.headers['range'] == f'bytes={offset}-'
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(206, headers={'Content-Range': f'bytes {offset}-{offset+2}/{offset+3}',
                                           'Content-Length': '3'}, stream=source)

    target = ProfileDistribution('https://example.invalid/nlp/', clock=lambda: 0,
                                 transport=httpx.MockTransport(respond))
    with target.stream_artifact('data/' + 'a'*64, offset=offset, size=offset+3,
                                check=lambda: None, deadline=10) as (start, blocks):
        assert start == offset and b''.join(blocks) == b'end'
    assert source.closed


def test_artifact_ignored_range_reports_full_restart():
    source = Incoming((b'whole',))
    with distribution(source).stream_artifact('data/' + 'a'*64, offset=2, size=5,
                                              check=lambda: None, deadline=10) as (start, blocks):
        assert start == 0 and b''.join(blocks) == b'whole'
    assert source.closed


def test_artifact_cancellation_prevents_buffering_later_network_blocks():
    cancelled = False

    def check():
        if cancelled:
            raise DependencyRejected('nlp_cancelled')

    source = Incoming((b'a', b'b', b'c'))
    with distribution(source).stream_artifact('data/' + 'a'*64, offset=0, size=3,
                                              check=check, deadline=10) as (_, blocks):
        assert next(blocks) == b'a'
        cancelled = True
        with pytest.raises(DependencyRejected, match='nlp_cancelled'):
            next(blocks)
    assert source.reads == 1 and source.closed


@pytest.mark.parametrize('status,headers', [
    (206, {'Content-Range': 'bytes 1-4/5'}),
    (206, {'Content-Range': 'bytes 2-5/6'}),
    (206, {'Content-Range': 'bytes 2-3/5'}),
    (206, {'Content-Range': 'bytes 2-4/*'}),
    (206, {'Content-Range': 'bytes 2-4/5, bytes 2-4/5'}),
    (206, {'Content-Range': 'bytes 2-4/5', 'Content-Length': '4'}),
    (206, {'Content-Range': 'bytes 2-4/5', 'Content-Encoding': 'gzip'}),
    (206, {'Content-Range': 'bytes 2-4/5', 'Content-Type': 'multipart/byteranges'}),
    (200, {'Content-Range': 'bytes 2-4/5'}),
    (416, {'Content-Range': 'bytes */5'}),
])
def test_artifact_refuses_inconsistent_range_before_reading(status, headers):
    source = Incoming((b'end',))
    target = ProfileDistribution('https://example.invalid/nlp/', clock=lambda: 0,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, headers=headers, stream=source)))
    with pytest.raises(DependencyRejected):
        with target.stream_artifact('data/' + 'a'*64, offset=2, size=5,
                                    check=lambda: None, deadline=10):
            pytest.fail('invalid response admitted')
    assert source.reads == 0 and source.closed


@pytest.mark.parametrize('body', [b'too long', b'no'])
def test_artifact_response_length_is_exact_without_content_length(body):
    source = Incoming((body,))
    with pytest.raises(DependencyRejected, match='nlp_distribution_size'):
        with distribution(source).stream_artifact('data/' + 'a'*64, offset=0, size=3,
                                                  check=lambda: None, deadline=10) as (_, blocks):
            list(blocks)
    assert source.closed
