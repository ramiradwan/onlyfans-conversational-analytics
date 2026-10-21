"""Fixed-origin bounded transport for opaque data; never executable delivery."""
from __future__ import annotations

from contextlib import contextmanager
import re
import time
from urllib.parse import urlsplit

import httpx

from app.nlp.release_catalog import require

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
        require(relative == 'catalog.jws' or re.fullmatch(r'(?:profiles|data)/[a-f0-9]{64}', relative),
                'nlp_distribution_path')

        def poll():
            check()
            require(self.clock() < deadline, 'nlp_acquisition_deadline')

        poll()
        with httpx.Client(follow_redirects=False, trust_env=False, transport=self.transport,
                          timeout=httpx.Timeout(30, connect=10)) as client:
            with client.stream('GET', self.base + relative, headers={'Accept-Encoding': 'identity'}) as response:
                require(response.status_code == 200 and response.headers.get('content-encoding', 'identity') == 'identity',
                        'nlp_distribution_response')

                def chunks():
                    for block in response.iter_raw(chunk_size=BLOCK_BYTES):
                        poll()
                        require(type(block) is bytes and len(block) <= BLOCK_BYTES, 'nlp_distribution_block')
                        yield block
                    poll()

                yield chunks()

    def read(self, relative, maximum, *, check, deadline):
        raw = bytearray()
        with self.stream(relative, check=check, deadline=deadline) as blocks:
            for block in blocks:
                require(len(raw) + len(block) <= maximum, 'nlp_distribution_size')
                raw.extend(block)
        return bytes(raw)
