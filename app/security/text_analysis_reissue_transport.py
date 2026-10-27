"""Credentialless HTTPS with raw byte bounds for native licence reissue."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time

import httpx

from app.security.text_analysis_reissue import (
    CHALLENGE_PATH, PATH_PREFIX, TextAnalysisReissueError,
)
from app.security.signed_objects import decode

ENTRY_PREFIX = "/v2/onboarding/text-analysis-license-reissues/"


def _origin(origin):
    try:
        if type(origin) is not str or not 0 < len(origin.encode("utf-8")) <= 2048:
            raise ValueError()
        url = httpx.URL(origin)
        valid = (url.scheme == "https" and url.host and "?" not in origin and "#" not in origin
                 and not url.username
                 and not url.password and not url.query and not url.fragment
                 and url.path in ("", "/"))
    except Exception:
        valid = False
    if not valid:
        raise TextAnalysisReissueError("invalid_origin")
    return str(url.copy_with(path="/")).rstrip("/")


def browser_entry_url(origin: str, entry_reference: str) -> str:
    selected = _origin(origin)
    try:
        if len(decode(entry_reference, 32)) != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise TextAnalysisReissueError("invalid_entry_reference") from None
    return selected + ENTRY_PREFIX + entry_reference


@dataclass(frozen=True)
class ReissueHTTPResponse:
    status_code: int
    body: bytes = field(repr=False)
    content_type: str
    cache_control: str
    content_encoding: str


class TextAnalysisReissueHTTPTransport:
    """Raw transport has no cookie jar, authentication or redirect dispatcher.

    timeout_seconds sets per-I/O inactivity timeouts and an accepted-response
    window. Blocking DNS, informational headers and HTTP framing may outlast
    that window; it is not a hard total call lifetime. Acceptance follows closure.
    """
    def __init__(self, origin: str, *, timeout_seconds: float = 10.0,
                 transport=None, monotonic=time.monotonic):
        selected = _origin(origin)
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0):
            raise TextAnalysisReissueError("invalid_timeout")
        self.origin = selected
        self._timeout = timeout_seconds
        self._monotonic = monotonic
        self._transport = transport if transport is not None else httpx.HTTPTransport(
            retries=0, trust_env=False)

    def request(self, path: str, raw: bytes, *, maximum: int, before_send=None) -> ReissueHTTPResponse:
        if type(path) is not str:
            raise TextAnalysisReissueError("invalid_path")
        if path == CHALLENGE_PATH:
            request_limit, response_limit = 12288, 4096
        elif path in {PATH_PREFIX + action for action in ("prepare", "read", "finalize", "recover")}:
            request_limit = 8192
            response_limit = 4096 if path.endswith(":prepare") else 24576
        else:
            raise TextAnalysisReissueError("invalid_path")
        if (type(raw) is not bytes or not 0 < len(raw) <= request_limit
                or type(maximum) is not int or maximum != response_limit):
            raise TextAnalysisReissueError("transport_bounds")
        started = self._monotonic()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise TextAnalysisReissueError("invalid_clock")
        deadline = started + self._timeout
        def check():
            now = self._monotonic()
            if type(now) not in (int, float) or not math.isfinite(now) or not started <= now < deadline:
                raise TextAnalysisReissueError("transport_deadline")
        if before_send is not None:
            before_send()
        check()
        # A low-level Request/Transport exchange performs no client cookie extraction.
        request = httpx.Request("POST", self.origin.rstrip("/") + path,
            headers={"Accept": "application/json", "Accept-Encoding": "identity",
                     "Content-Type": "application/json"}, content=raw,
            extensions={"timeout": httpx.Timeout(self._timeout).as_dict()})
        response = None
        try:
            response = self._transport.handle_request(request)
            check()
            content_types = response.headers.get_list("content-type")
            cache_controls = response.headers.get_list("cache-control")
            encodings = response.headers.get_list("content-encoding")
            if (len(content_types) != 1 or len(cache_controls) != 1 or len(encodings) > 1
                    or len(content_types[0]) > 256 or len(cache_controls[0]) > 256
                    or (encodings and len(encodings[0]) > 64)
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
                    or cache_controls[0].strip().lower() != "no-store"
                    or (encodings and encodings[0].strip().lower() != "identity")):
                raise TextAnalysisReissueError("invalid_transport_response")
            limit = maximum if response.status_code in (200, 201) else min(maximum, 4096)
            lengths = response.headers.get_list("content-length")
            if lengths and (len(lengths) != 1 or not lengths[0].isascii()
                            or not lengths[0].isdigit() or len(lengths[0]) > 10
                            or int(lengths[0]) > limit):
                raise TextAnalysisReissueError("response_size")
            body = bytearray()
            for chunk in response.iter_raw():
                check()
                if len(body) + len(chunk) > limit:
                    raise TextAnalysisReissueError("response_size")
                body.extend(chunk)
            check()
            if lengths and len(body) != int(lengths[0]):
                raise TextAnalysisReissueError("response_size")
            result = ReissueHTTPResponse(response.status_code, bytes(body), content_types[0],
                                         cache_controls[0], encodings[0] if encodings else "identity")
        except TextAnalysisReissueError:
            raise
        except Exception:
            raise TextAnalysisReissueError("transport_unavailable") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    raise TextAnalysisReissueError("transport_close_failed") from None
        check()
        return result

    def close(self):
        self._transport.close()
