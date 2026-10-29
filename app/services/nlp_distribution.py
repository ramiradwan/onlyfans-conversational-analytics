"""Fixed-origin bounded transport for opaque data; never executable delivery."""
from __future__ import annotations

from contextlib import contextmanager
import re
import time
from urllib.parse import urlsplit

import httpx

from app.nlp.release_catalog import require
from app.nlp.profiles import MAX_SAFE_INTEGER

ACQUISITION_SECONDS = 3600
BLOCK_BYTES = 65536


class ProfileDistribution:
    def __init__(self, base_url, *, transport=None, clock=time.monotonic):
        parsed = urlsplit(base_url)
        require(parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443)
                and not parsed.query and not parsed.fragment and base_url.endswith('/')
                and '\\' not in base_url and not any(ord(c) <= 32 for c in base_url), 'nlp_distribution_origin')
        self.base, self.transport, self.clock = base_url, transport, clock

    @contextmanager
    def stream(self, relative, *, check, deadline):
        with self._stream(relative, check=check, deadline=deadline) as (_, blocks):
            yield blocks

    @contextmanager
    def stream_artifact(self, relative, *, offset, size, check, deadline):
        require(re.fullmatch(r'data/[a-f0-9]{64}', relative)
                and type(size) is int and 0 < size <= MAX_SAFE_INTEGER
                and type(offset) is int and 0 <= offset < size, 'nlp_distribution_range')
        with self._stream(relative, offset=offset, size=size, check=check, deadline=deadline) as result:
            yield result

    @contextmanager
    def _stream(self, relative, *, check, deadline, offset=0, size=None):
        require(relative == 'catalog.jws' or re.fullmatch(r'(?:profiles|data)/[a-f0-9]{64}', relative),
                'nlp_distribution_path')

        def poll():
            check()
            require(self.clock() < deadline, 'nlp_acquisition_deadline')

        poll()
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        with httpx.Client(follow_redirects=False, trust_env=False, transport=self.transport,
                          timeout=httpx.Timeout(30, connect=10)) as client:
            with client.stream('GET', self.base + relative, headers=headers) as response:
                require(response.headers.get('content-encoding', 'identity') == 'identity'
                        and not response.headers.get('content-type', '').lower().startswith('multipart/'),
                        'nlp_distribution_response')
                start = 0
                if offset and response.status_code == 206:
                    match = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)',
                                         response.headers.get('content-range', ''))
                    require(match is not None and tuple(map(int, match.groups())) == (offset, size - 1, size),
                            'nlp_distribution_range')
                    start = offset
                else:
                    require(response.status_code == 200 and 'content-range' not in response.headers,
                            'nlp_distribution_response')
                if size is not None and 'content-length' in response.headers:
                    length = response.headers['content-length']
                    require(re.fullmatch(r'[0-9]+', length) and int(length) == size - start,
                            'nlp_distribution_size')

                def chunks():
                    raw = response.iter_raw()
                    count = start
                    while True:
                        poll()
                        try:
                            block = next(raw)
                        except StopIteration:
                            break
                        require(type(block) is bytes, 'nlp_distribution_block')
                        for index in range(0, len(block), BLOCK_BYTES):
                            poll()
                            part = block[index:index + BLOCK_BYTES]
                            count += len(part)
                            require(size is None or count <= size, 'nlp_distribution_size')
                            yield part
                    poll()
                    require(size is None or count == size, 'nlp_distribution_size')

                yield start, chunks()

    def read(self, relative, maximum, *, check, deadline):
        raw = bytearray()
        with self.stream(relative, check=check, deadline=deadline) as blocks:
            for block in blocks:
                require(len(raw) + len(block) <= maximum, 'nlp_distribution_size')
                raw.extend(block)
        return bytes(raw)
