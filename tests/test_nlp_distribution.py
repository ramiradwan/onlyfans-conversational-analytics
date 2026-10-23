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
