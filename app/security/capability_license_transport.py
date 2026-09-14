"""HTTPS transport for CapabilityLicense delivery requests."""

from __future__ import annotations

import math
from time import monotonic
from typing import Mapping

import httpx

from app.security.hosted_grants import TransportResponse

_MAX_RESPONSE_BYTES = 65_536


class CapabilityLicenseHTTPTransport:
    """Bounded HTTPS JSON transport with per-request header support."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Hosted timeout must be finite and positive")
        self._timeout_seconds = timeout_seconds
        try:
            url = httpx.URL(base_url)
        except Exception:
            raise ValueError("Hosted base URL is invalid") from None
        if (
            url.scheme != "https"
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Hosted base URL must be an HTTPS origin")
        self._client = httpx.Client(
            base_url=str(url.copy_with(path="/")),
            follow_redirects=False,
            timeout=timeout_seconds,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        deadline = monotonic() + self._timeout_seconds
        try:
            with self._client.stream(
                method,
                path,
                json=json_body,
                headers=headers,
            ) as response:
                if monotonic() >= deadline:
                    raise RuntimeError("Hosted response exceeded deadline")
                if (
                    response.headers.get("content-encoding", "identity")
                    .strip()
                    .lower()
                    != "identity"
                ):
                    raise RuntimeError("Hosted response encoding is unsupported")
                body = bytearray()
                for chunk in response.iter_raw():
                    if monotonic() >= deadline:
                        raise RuntimeError("Hosted response exceeded deadline")
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise RuntimeError("Hosted response exceeds size limit")
                    body.extend(chunk)
                if monotonic() >= deadline:
                    raise RuntimeError("Hosted response exceeded deadline")
                return TransportResponse(
                    response.status_code,
                    bytes(body),
                    response.headers.get("content-type", ""),
                )
        except Exception:
            raise RuntimeError("Hosted request failed") from None

    def close(self) -> None:
        self._client.close()
