"""Proof-bound opaque Hosted continuation redemption for CapabilityLicense delivery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import uuid4

from app.persistence.auth import (
    AuthenticationStore,
    InstallationKeyReference,
    SQLiteAuthenticationStore,
)
from app.security.capability_license_composition import (
    CapabilityLicenseDeliveryReceipt,
    CapabilityLicenseDeliveryResult,
    CapabilityLicenseLocalDelivery,
    durable_capability_license_delivery,
)
from app.security.capability_license_delivery import CapabilityLicenseTransport
from app.security.capability_license_delivery_journal import (
    SQLiteCapabilityLicenseDeliveryJournal,
)
from app.security.capability_license_transport import CapabilityLicenseHTTPTransport
from app.security.grant_types import ACTIVATION_GRANT_TYPES
from app.security.hosted_grants import InstallationProofAuthority, TransportResponse
from app.security.installation_key import (
    InstallationKeyAuthority,
    InstallationKeyError,
    WindowsCNGInstallationKeyProvider,
)

_CONTINUATION_PROFILE = "urn:bridge-clean:capability-license-redemption-continuation:v1"
_PROOF_PROFILE = "urn:bridge-clean:capability-license-redemption-proof:v1"
_REDEMPTION_PROFILE = "urn:bridge-clean:capability-license-redemption:v1"
_PROOF_AUDIENCE = "urn:bridge-clean:commercial-control-plane:capability-license-redemption"
_PROOF_DOMAIN = b"BRIDGE-CLEAN-CAPABILITY-LICENSE-REDEMPTION-PROOF-V1\x00"
_PROOF_PATH = "/v1/capability-license-redemption-proof-challenges"
_REDEMPTION_PATH = "/v1/capability-license-redemptions"
_PURPOSE = "capability-license-redeem"
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_CONTINUATION = re.compile(r"^clr1\.[A-Za-z0-9_-]{43}$")
_UUIDV7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
_B64U = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_AUTHORITY_REFERENCES = 128

CapabilityLicenseRedemptionRefusal = Literal[
    "redemption_expired",
    "redemption_invalid",
    "redemption_unauthorized",
    "redemption_mismatch",
    "redemption_conflict",
    "reissue_authorization_required",
    "reissue_authorization_unavailable",
    "local_installation_authority_unavailable",
    "installation_key_unavailable",
    "durable_store_unavailable",
    "hosted_origin_unavailable",
    "hosted_unavailable",
]
CapabilityLicenseOpaqueRedemptionResult = (
    CapabilityLicenseDeliveryReceipt
    | CapabilityLicenseRedemptionRefusal
    | CapabilityLicenseDeliveryResult
)


class CapabilityLicenseOpaqueRedemption(Protocol):
    def redeem(self, *, continuation: str) -> CapabilityLicenseOpaqueRedemptionResult: ...


class CapabilityLicenseRedemptionError(RuntimeError):
    """Base error that never implies local CapabilityLicense installation."""


class CapabilityLicenseRedemptionUnavailable(CapabilityLicenseRedemptionError):
    """Transport or malformed Hosted output did not yield an authoritative result."""


class CapabilityLicenseRedemptionRefused(CapabilityLicenseRedemptionError):
    """Hosted authoritatively refused the bound continuation."""

    def __init__(self, result: CapabilityLicenseRedemptionRefusal) -> None:
        super().__init__("CapabilityLicense redemption was refused")
        self.result = result


@dataclass(frozen=True, slots=True)
class ProtectedDelivery:
    """Brain-only ADR 0020 delivery coordinates returned by Hosted redemption."""

    redemption_id: str
    hosted_result: Literal["accepted", "already_completed"]
    operation: Literal["activation", "reissue"]
    package: str
    seat_id: str


class CapabilityLicenseRedemptionClient:
    """Redeem one opaque continuation with the exact current local installation key."""

    def __init__(
        self,
        transport: CapabilityLicenseTransport,
        installation_key: InstallationProofAuthority,
    ) -> None:
        self._transport = transport
        self._installation_key = installation_key

    def redeem(
        self,
        continuation: str,
        *,
        organization_id: str,
        installation_id: str,
        key: InstallationKeyReference,
    ) -> ProtectedDelivery:
        if _CONTINUATION.fullmatch(continuation) is None:
            raise CapabilityLicenseRedemptionRefused("redemption_invalid")
        unavailable: CapabilityLicenseRedemptionUnavailable | None = None
        for _attempt in range(2):
            try:
                return self._redeem_once(
                    continuation,
                    organization_id=organization_id,
                    installation_id=installation_id,
                    key=key,
                )
            except CapabilityLicenseRedemptionUnavailable as error:
                unavailable = error
        assert unavailable is not None
        raise unavailable

    def _redeem_once(
        self,
        continuation: str,
        *,
        organization_id: str,
        installation_id: str,
        key: InstallationKeyReference,
    ) -> ProtectedDelivery:
        challenge_response = self._request(
            _PROOF_PATH,
            {
                "profile": _PROOF_PROFILE,
                "continuation": continuation,
                "organization_id": organization_id,
                "installation_id": installation_id,
            },
        )
        if _retryable(challenge_response.status_code):
            raise CapabilityLicenseRedemptionUnavailable("redemption challenge unavailable")
        if challenge_response.status_code != 201:
            self._raise_refusal(challenge_response)
        challenge = _response_object(challenge_response)
        expected_challenge = {
            "profile",
            "continuation",
            "redemption_id",
            "organization_id",
            "installation_id",
            "purpose",
            "proof_challenge_id",
            "challenge",
            "audience",
            "issued_at",
            "expires_at",
        }
        challenge_bytes = challenge.get("challenge")
        if (
            set(challenge) != expected_challenge
            or challenge.get("profile") != _PROOF_PROFILE
            or challenge.get("continuation") != continuation
            or challenge.get("organization_id") != organization_id
            or challenge.get("installation_id") != installation_id
            or challenge.get("purpose") != _PURPOSE
            or challenge.get("audience") != _PROOF_AUDIENCE
            or not _uuid(challenge.get("redemption_id"))
            or not _uuid(challenge.get("proof_challenge_id"))
            or not _b64u32(challenge_bytes)
            or not _timestamp(challenge.get("issued_at"))
            or not _timestamp(challenge.get("expires_at"))
        ):
            raise CapabilityLicenseRedemptionUnavailable("redemption challenge invalid")

        request: dict[str, object] = {
            "profile": _REDEMPTION_PROFILE,
            "continuation": continuation,
            "redemption_id": challenge["redemption_id"],
            "organization_id": organization_id,
            "installation_id": installation_id,
        }
        body = {
            "request": request,
            "proof": self._proof(
                challenge=challenge_bytes,
                request_body=request,
                organization_id=organization_id,
                installation_id=installation_id,
                key=key,
            ),
        }
        response = self._request(
            _REDEMPTION_PATH,
            body,
            headers={"Idempotency-Key": str(uuid4())},
        )
        if _retryable(response.status_code):
            raise CapabilityLicenseRedemptionUnavailable("redemption result unavailable")
        if response.status_code not in {200, 201}:
            self._raise_refusal(response)
        document = _response_object(response)
        expected = {
            "profile",
            "redemption_id",
            "result",
            "authorization_state",
            "delivery",
        }
        delivery = document.get("delivery")
        if (
            set(document) != expected
            or document.get("profile") != _REDEMPTION_PROFILE
            or document.get("redemption_id") != challenge["redemption_id"]
            or document.get("result") not in {"accepted", "already_completed"}
            or document.get("authorization_state") != "delivery_available"
            or not isinstance(delivery, dict)
            or set(delivery) != {"operation", "package", "seat_id"}
            or delivery.get("operation") not in {"activation", "reissue"}
            or not isinstance(delivery.get("package"), str)
            or not 1 <= len(delivery["package"]) <= 1400
            or _B64U.fullmatch(delivery["package"]) is None
            or not _identifier(delivery.get("seat_id"))
            or (response.status_code == 201 and document.get("result") != "accepted")
            or (response.status_code == 200 and document.get("result") != "already_completed")
        ):
            raise CapabilityLicenseRedemptionUnavailable("redemption response invalid")
        operation = delivery["operation"]
        hosted_result = document["result"]
        assert isinstance(operation, str) and isinstance(hosted_result, str)
        return ProtectedDelivery(
            redemption_id=str(document["redemption_id"]),
            hosted_result=hosted_result,  # type: ignore[arg-type]
            operation=operation,  # type: ignore[arg-type]
            package=str(delivery["package"]),
            seat_id=str(delivery["seat_id"]),
        )

    def _proof(
        self,
        *,
        challenge: str,
        request_body: Mapping[str, object],
        organization_id: str,
        installation_id: str,
        key: InstallationKeyReference,
    ) -> dict[str, str]:
        values = (
            _b64u_decode(challenge),
            b"POST",
            _REDEMPTION_PATH.encode("ascii"),
            hashlib.sha256(_canonical_json(request_body)).digest(),
            _PURPOSE.encode("ascii"),
            installation_id.encode("utf-8"),
            organization_id.encode("utf-8"),
            key.installation_key_id.encode("utf-8"),
            _PROOF_AUDIENCE.encode("ascii"),
        )
        canonical = bytearray(_PROOF_DOMAIN)
        for value in values:
            canonical += struct.pack("!I", len(value)) + value
        proof = self._installation_key.sign_challenge(bytes(canonical))
        if (
            proof.installation_key_id != key.installation_key_id
            or proof.algorithm != "ES256"
            or len(proof.signature) != 64
            or int.from_bytes(proof.signature[32:], "big") > _P256_ORDER // 2
        ):
            raise CapabilityLicenseRedemptionUnavailable("redemption installation proof invalid")
        return {
            "challenge": challenge,
            "key_id": key.installation_key_id,
            "signature": _b64u(proof.signature),
        }

    def _request(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        try:
            response = self._transport.request(
                "POST",
                path,
                json_body=body,
                headers=headers,
            )
        except Exception as error:
            raise CapabilityLicenseRedemptionUnavailable("redemption transport failed") from error
        if (
            not isinstance(response, TransportResponse)
            or not isinstance(response.status_code, int)
            or isinstance(response.status_code, bool)
            or not isinstance(response.body, bytes)
            or len(response.body) > 65_536
            or response.content_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise CapabilityLicenseRedemptionUnavailable("redemption transport response invalid")
        return response

    @staticmethod
    def _raise_refusal(response: TransportResponse) -> None:
        detail = _error_detail(response)
        if response.status_code == 410 or detail == "continuation_expired":
            raise CapabilityLicenseRedemptionRefused("redemption_expired")
        if response.status_code == 404 or detail == "continuation_invalid":
            raise CapabilityLicenseRedemptionRefused("redemption_invalid")
        if response.status_code in {401, 403}:
            raise CapabilityLicenseRedemptionRefused("redemption_unauthorized")
        if detail in {"installation_binding_mismatch", "organization_binding_mismatch"}:
            raise CapabilityLicenseRedemptionRefused("redemption_mismatch")
        if detail == "reissue_authorization_required":
            raise CapabilityLicenseRedemptionRefused("reissue_authorization_required")
        if detail == "reissue_authorization_unavailable":
            raise CapabilityLicenseRedemptionRefused("reissue_authorization_unavailable")
        if response.status_code == 400:
            raise CapabilityLicenseRedemptionRefused("redemption_invalid")
        if response.status_code == 409:
            raise CapabilityLicenseRedemptionRefused("redemption_conflict")
        raise CapabilityLicenseRedemptionUnavailable("redemption refusal response invalid")


TransportFactory = Callable[[str, SQLiteCapabilityLicenseDeliveryJournal], CapabilityLicenseTransport]
ProofAuthorityFactory = Callable[[AuthenticationStore], InstallationProofAuthority]
DeliveryFactory = Callable[[Callable[[], AuthenticationStore], str], CapabilityLicenseLocalDelivery]


def _production_transport(
    hosted_origin: str,
    journal: SQLiteCapabilityLicenseDeliveryJournal,
) -> CapabilityLicenseTransport:
    return CapabilityLicenseHTTPTransport(hosted_origin, journal=journal)


def _production_proof_authority(store: AuthenticationStore) -> InstallationProofAuthority:
    return InstallationKeyAuthority(store, WindowsCNGInstallationKeyProvider())


def _production_delivery(
    open_store: Callable[[], AuthenticationStore],
    hosted_origin: str,
) -> CapabilityLicenseLocalDelivery:
    return durable_capability_license_delivery(open_store, hosted_origin=hosted_origin)


def durable_capability_license_opaque_redemption(
    open_store: Callable[[], AuthenticationStore],
    *,
    hosted_origin: str,
    transport_factory: TransportFactory = _production_transport,
    proof_authority_factory: ProofAuthorityFactory = _production_proof_authority,
    delivery_factory: DeliveryFactory = _production_delivery,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CapabilityLicenseOpaqueRedemption:
    """Build the thin opaque redemption action over the existing delivery authority."""

    class _Action:
        def redeem(self, *, continuation: str) -> CapabilityLicenseOpaqueRedemptionResult:
            if _CONTINUATION.fullmatch(continuation) is None:
                return "redemption_invalid"
            store = open_store()
            if not isinstance(store, SQLiteAuthenticationStore):
                return "durable_store_unavailable"
            try:
                proof_authority = proof_authority_factory(store)
                key = proof_authority.ensure_ready()
            except InstallationKeyError:
                return "installation_key_unavailable"
            coordinates = _current_local_coordinates(store, key=key, instant=now())
            if coordinates is None:
                return "local_installation_authority_unavailable"
            organization_id, installation_id = coordinates
            journal = SQLiteCapabilityLicenseDeliveryJournal(store.database)
            try:
                transport = transport_factory(hosted_origin, journal)
            except ValueError:
                return "hosted_origin_unavailable"
            try:
                protected = CapabilityLicenseRedemptionClient(
                    transport,
                    proof_authority,
                ).redeem(
                    continuation,
                    organization_id=organization_id,
                    installation_id=installation_id,
                    key=key,
                )
            except CapabilityLicenseRedemptionRefused as refusal:
                return refusal.result
            except (CapabilityLicenseRedemptionUnavailable, InstallationKeyError):
                return "hosted_unavailable"
            finally:
                close = getattr(transport, "close", None)
                if callable(close):
                    close()

            # Protected package and seat coordinates remain inside Brain and are
            # immediately delegated to the existing verify-before-persist,
            # crash-safe CapabilityLicense delivery authority.
            delivery = delivery_factory(open_store, hosted_origin)
            if protected.operation == "activation":
                return delivery.activate(
                    package=protected.package,
                    seat_id=protected.seat_id,
                )
            return delivery.finalize_reissue(
                package=protected.package,
                seat_id=protected.seat_id,
            )

    return _Action()


def _current_local_coordinates(
    store: SQLiteAuthenticationStore,
    *,
    key: InstallationKeyReference,
    instant: datetime,
) -> tuple[str, str] | None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("CapabilityLicense redemption clock must be timezone-aware")
    grants = store.verified_grants(limit=_MAX_AUTHORITY_REFERENCES + 1)
    if len(grants) > _MAX_AUTHORITY_REFERENCES:
        return None
    required = set(ACTIVATION_GRANT_TYPES)
    grouped: dict[tuple[str, str, str, str], set[str]] = {}
    for grant in grants:
        if (
            grant.grant_type not in required
            or not (grant.valid_from <= instant < grant.expires_at)
            or grant.installation_key_id != key.installation_key_id
            or grant.installation_key_jkt != key.installation_key_jkt
        ):
            continue
        coordinate = (
            grant.organization_id,
            grant.installation_id,
            grant.issuer,
            grant.subject,
        )
        grouped.setdefault(coordinate, set()).add(grant.grant_type)
    complete = [coordinate for coordinate, types in grouped.items() if types == required]
    if len(complete) != 1:
        return None
    organization_id, installation_id, _issuer, _subject = complete[0]
    return organization_id, installation_id


def _response_object(response: TransportResponse) -> dict[str, object]:
    try:
        value = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise CapabilityLicenseRedemptionUnavailable("redemption response JSON invalid") from None
    if not isinstance(value, dict):
        raise CapabilityLicenseRedemptionUnavailable("redemption response JSON invalid")
    return value


def _error_detail(response: TransportResponse) -> str | None:
    try:
        document = _response_object(response)
    except CapabilityLicenseRedemptionUnavailable:
        return None
    detail = document.get("detail")
    return detail if isinstance(detail, str) else None


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _b64u32(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(_b64u_decode(value)) == 32
    except ValueError:
        return False


def _b64u_decode(value: str) -> bytes:
    if _B64U.fullmatch(value) is None or "=" in value:
        raise ValueError("invalid base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid base64url") from error
    if _b64u(raw) != value:
        raise ValueError("noncanonical base64url")
    return raw


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _retryable(status: int) -> bool:
    return status in {408, 425, 429} or status >= 500


def _uuid(value: object) -> bool:
    return isinstance(value, str) and _UUIDV7.fullmatch(value) is not None


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _timestamp(value: object) -> bool:
    return isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None
