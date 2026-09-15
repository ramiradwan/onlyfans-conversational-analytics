"""HTTPS transport for crash-safe CapabilityLicense delivery requests."""

from __future__ import annotations

import json
import math
from time import monotonic
from typing import Mapping, Protocol

import httpx

from app.security.capability_license_delivery_journal import (
    SQLiteCapabilityLicenseDeliveryJournal,
)
from app.security.hosted_grants import TransportResponse

_MAX_RESPONSE_BYTES = 65_536


class _Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse: ...


class DurableCapabilityLicenseTransport:
    """Replay commit operations from durable state across Brain restarts."""

    def __init__(
        self,
        transport: _Transport,
        journal: SQLiteCapabilityLicenseDeliveryJournal,
    ) -> None:
        self._transport = transport
        self._journal = journal

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        identity = _commit_identity(method, path, json_body)
        if identity is None:
            return self._transport.request(
                method,
                path,
                json_body=json_body,
                headers=headers,
            )

        operation_type, authority_reference_id = identity
        supplied_key = (headers or {}).get("Idempotency-Key", "").strip()
        if not supplied_key:
            raise RuntimeError(
                "CapabilityLicense commit request requires Idempotency-Key"
            )

        pending = self._journal.prepare(
            operation_type,
            authority_reference_id,
            path,
            supplied_key,
            json_body,
        )
        replay_headers = dict(headers or {})
        replay_headers["Idempotency-Key"] = pending.idempotency_key
        response = self._transport.request(
            method,
            pending.endpoint,
            json_body=pending.request_body,
            headers=replay_headers,
        )
        if response.status_code == 201:
            token = _response_capability_license(response)
            if token is not None:
                self._journal.bind_committed_response(pending, token)
        return response


class _RawCapabilityLicenseHTTPTransport:
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


class CapabilityLicenseHTTPTransport:
    """Production HTTPS transport with mandatory durable commit recovery."""

    def __init__(
        self,
        base_url: str,
        *,
        journal: SQLiteCapabilityLicenseDeliveryJournal,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._http = _RawCapabilityLicenseHTTPTransport(
            base_url,
            timeout_seconds=timeout_seconds,
        )
        self._durable = DurableCapabilityLicenseTransport(self._http, journal)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        return self._durable.request(
            method,
            path,
            json_body=json_body,
            headers=headers,
        )

    def close(self) -> None:
        close = getattr(self._http, "close", None)
        if callable(close):
            close()


def _commit_identity(
    method: str,
    path: str,
    body: Mapping[str, object],
) -> tuple[str, str] | None:
    if method != "POST":
        return None
    if path.endswith(":activate"):
        operation_type = "activate"
        reference_field = "exchange_id"
    elif path.endswith(":finalize"):
        operation_type = "finalize"
        reference_field = "reissue_authorization_id"
    else:
        return None

    request = body.get("request")
    reference = request.get(reference_field) if isinstance(request, Mapping) else None
    if not isinstance(reference, str) or not reference:
        raise RuntimeError("CapabilityLicense commit authority reference is missing")
    return operation_type, reference


def _response_capability_license(response: TransportResponse) -> str | None:
    if (
        response.content_type.split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        return None
    try:
        document = json.loads(response.body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    token = document.get("capability_license")
    return token if isinstance(token, str) and token else None
