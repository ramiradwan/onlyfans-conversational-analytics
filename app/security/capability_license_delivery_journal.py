"""Durable pending journal for crash-safe CapabilityLicense delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Mapping

from app.persistence.database import AuthSQLite
from app.security.capability_license_redemption_journal import provider_origin

DeliveryOperation = Literal["activate", "finalize"]


class CapabilityLicenseDeliveryJournalError(RuntimeError):
    """Pending delivery state is unavailable, corrupt, or inconsistent."""


class CapabilityLicenseDeliveryCompleted(CapabilityLicenseDeliveryJournalError):
    """Completion won the race; reverify its recorded authority before returning."""


@dataclass(frozen=True, slots=True)
class PendingCapabilityLicenseDelivery:
    operation_type: DeliveryOperation
    authority_reference_id: str
    endpoint: str
    idempotency_key: str
    request_body: dict[str, object]
    request_digest: str
    response_object_digest: str | None
    created_at: str
    provider_origin: str


class SQLiteCapabilityLicenseDeliveryJournal:
    """Persist exact commit requests in the encrypted auth database."""

    def __init__(
        self,
        database: AuthSQLite,
        origin: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self.origin = provider_origin(origin)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(
        self,
        operation_type: DeliveryOperation,
        authority_reference_id: str,
        endpoint: str,
        idempotency_key: str,
        request_body: Mapping[str, object],
    ) -> PendingCapabilityLicenseDelivery:
        if operation_type not in {"activate", "finalize"}:
            raise ValueError("CapabilityLicense delivery operation is invalid")
        if not authority_reference_id or not endpoint or not idempotency_key:
            raise ValueError("CapabilityLicense delivery identity is incomplete")

        encoded = _canonical_request(request_body)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        created_at = _timestamp(self._clock())

        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT operation_type, authority_reference_id, endpoint,
                       idempotency_key, request_body, request_digest,
                       response_object_digest, created_at, provider_origin
                FROM capability_license_pending_deliveries
                WHERE operation_type = ? AND authority_reference_id = ?
                """,
                (operation_type, authority_reference_id),
            ).fetchone()
            if row is None:
                if connection.execute(
                    "SELECT 1 FROM capability_license_redemption_journal "
                    "WHERE provider_origin=? AND delivery_operation=? AND authority_reference_id=? "
                    "AND completed_reference_id IS NOT NULL LIMIT 1",
                    (self.origin, operation_type, authority_reference_id),
                ).fetchone() is not None:
                    raise CapabilityLicenseDeliveryCompleted("Delivery already completed")
                connection.execute(
                    """
                    INSERT INTO capability_license_pending_deliveries (
                        operation_type, authority_reference_id, endpoint,
                        idempotency_key, request_body, request_digest,
                        response_object_digest, state, created_at, provider_origin
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
                    """,
                    (
                        operation_type,
                        authority_reference_id,
                        endpoint,
                        idempotency_key,
                        encoded,
                        digest,
                        created_at,
                        self.origin,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT operation_type, authority_reference_id, endpoint,
                           idempotency_key, request_body, request_digest,
                           response_object_digest, created_at, provider_origin
                    FROM capability_license_pending_deliveries
                    WHERE operation_type = ? AND authority_reference_id = ?
                    """,
                    (operation_type, authority_reference_id),
                ).fetchone()

        if row is None:
            raise CapabilityLicenseDeliveryJournalError(
                "CapabilityLicense pending delivery was not persisted"
            )
        pending = _pending_from_row(row)
        if pending.endpoint != endpoint or pending.provider_origin != self.origin:
            raise CapabilityLicenseDeliveryJournalError(
                "CapabilityLicense pending delivery endpoint changed"
            )
        return pending

    def bind_committed_response(
        self,
        pending: PendingCapabilityLicenseDelivery,
        capability_license: str,
    ) -> str:
        if not capability_license:
            raise CapabilityLicenseDeliveryJournalError(
                "Committed CapabilityLicense response is missing signed authority"
            )
        try:
            token_bytes = capability_license.encode("ascii")
        except UnicodeEncodeError as error:
            raise CapabilityLicenseDeliveryJournalError(
                "Committed CapabilityLicense is not ASCII JWS"
            ) from error
        object_digest = hashlib.sha256(token_bytes).hexdigest()
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT idempotency_key, response_object_digest
                FROM capability_license_pending_deliveries
                WHERE operation_type = ? AND authority_reference_id = ?
                """,
                (pending.operation_type, pending.authority_reference_id),
            ).fetchone()
            if row is None and connection.execute(
                "SELECT 1 FROM capability_license_redemption_journal "
                "WHERE provider_origin=? AND delivery_operation=? AND authority_reference_id=? "
                "AND response_object_digest=? AND completed_reference_id IS NOT NULL LIMIT 1",
                (self.origin, pending.operation_type, pending.authority_reference_id, object_digest),
            ).fetchone() is not None:
                raise CapabilityLicenseDeliveryCompleted("Delivery already completed")
            if row is None or str(row["idempotency_key"]) != pending.idempotency_key:
                raise CapabilityLicenseDeliveryJournalError(
                    "CapabilityLicense pending delivery changed before response binding"
                )
            prior = row["response_object_digest"]
            if prior is not None and str(prior) != object_digest:
                raise CapabilityLicenseDeliveryJournalError(
                    "CapabilityLicense committed response changed across replay"
                )
            connection.execute(
                """
                UPDATE capability_license_pending_deliveries
                SET response_object_digest = ?
                WHERE operation_type = ? AND authority_reference_id = ?
                """,
                (
                    object_digest,
                    pending.operation_type,
                    pending.authority_reference_id,
                ),
            )
            request = pending.request_body.get("request", {})
            proof = pending.request_body.get("proof", {})
            connection.execute("UPDATE capability_license_redemption_journal SET response_object_digest=? "
                "WHERE provider_origin=? AND delivery_operation=? AND authority_reference_id=? "
                "AND json_extract(scope_json,'$.organization_id')=? "
                "AND json_extract(scope_json,'$.installation_id')=? "
                "AND json_extract(scope_json,'$.installation_key_id')=?",
                (object_digest, self.origin, pending.operation_type, pending.authority_reference_id,
                    request.get("organization_id"), request.get("installation_id", request.get("replacement_installation_id")),
                    proof.get("key_id")))
        return object_digest

    def pending(
        self,
        operation_type: DeliveryOperation,
        authority_reference_id: str,
    ) -> PendingCapabilityLicenseDelivery | None:
        with self._database.read() as connection:
            row = connection.execute(
                """
                SELECT operation_type, authority_reference_id, endpoint,
                       idempotency_key, request_body, request_digest,
                       response_object_digest, created_at, provider_origin
                FROM capability_license_pending_deliveries
                WHERE operation_type = ? AND authority_reference_id = ?
                """,
                (operation_type, authority_reference_id),
            ).fetchone()
        pending = None if row is None else _pending_from_row(row)
        if pending is not None and pending.provider_origin != self.origin:
            raise CapabilityLicenseDeliveryJournalError("Original delivery Provider changed")
        return pending


def _pending_from_row(row: object) -> PendingCapabilityLicenseDelivery:
    try:
        operation_type = str(row["operation_type"])  # type: ignore[index]
        authority_reference_id = str(row["authority_reference_id"])  # type: ignore[index]
        endpoint = str(row["endpoint"])  # type: ignore[index]
        idempotency_key = str(row["idempotency_key"])  # type: ignore[index]
        request_text = str(row["request_body"])  # type: ignore[index]
        request_digest = str(row["request_digest"])  # type: ignore[index]
        response_digest_raw = row["response_object_digest"]  # type: ignore[index]
        created_at = str(row["created_at"])  # type: ignore[index]
        origin = provider_origin(row["provider_origin"])  # type: ignore[index]
    except Exception as error:
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending delivery row is invalid"
        ) from error

    if operation_type not in {"activate", "finalize"}:
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending delivery operation is invalid"
        )
    if hashlib.sha256(request_text.encode("utf-8")).hexdigest() != request_digest:
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending delivery request digest mismatch"
        )
    try:
        request_body = json.loads(request_text)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending delivery request is not valid JSON"
        ) from error
    if not isinstance(request_body, dict):
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending delivery request is not an object"
        )
    response_object_digest = (
        None if response_digest_raw is None else str(response_digest_raw)
    )
    return PendingCapabilityLicenseDelivery(
        operation_type=operation_type,  # type: ignore[arg-type]
        authority_reference_id=authority_reference_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_body=request_body,
        request_digest=request_digest,
        response_object_digest=response_object_digest,
        created_at=created_at,
        provider_origin=origin,
    )


def _canonical_request(request_body: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            request_body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending request is not JSON-safe"
        ) from error
    if not isinstance(decoded, dict):
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending request must be an object"
        )
    if len(encoded.encode("utf-8")) > 65_536:
        raise CapabilityLicenseDeliveryJournalError(
            "CapabilityLicense pending request exceeds size limit"
        )
    return encoded


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("CapabilityLicense delivery journal clock must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
