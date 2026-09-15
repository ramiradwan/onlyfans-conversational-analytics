"""Shipping composition for local CapabilityLicense activation and reissue delivery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Protocol

from app.persistence.auth import (
    AuthenticationStore,
    InstallationKeyReference,
    SQLiteAuthenticationStore,
    VerifiedCapabilityLicenseReference,
)
from app.security.capability_license_delivery_journal import (
    SQLiteCapabilityLicenseDeliveryJournal,
)
from app.security.analysis_authorization import (
    _ANALYSIS_ARTIFACT_FAMILY as ANALYSIS_ARTIFACT_FAMILY,
    _ANALYSIS_CAPABILITY as ANALYSIS_CAPABILITY,
    _ANALYSIS_MAJOR_VERSION as ANALYSIS_MAJOR_VERSION,
)
from app.security.capability_license_delivery import (
    CapabilityLicenseDeliveryClient,
    CapabilityLicenseDeliveryRefused,
    CapabilityLicenseDeliveryUnavailable,
    CapabilityLicenseTransport,
    decode_activation_package,
    decode_reissue_package,
)
from app.security.capability_license_transport import CapabilityLicenseHTTPTransport
from app.security.capability_license_verifier import (
    CapabilityLicenseAuthorityService,
    CapabilityLicenseVerificationContext,
    CapabilityLicenseVerificationError,
    CapabilityLicenseVerifier,
)
from app.security.grant_types import ACTIVATION_GRANT_TYPES
from app.security.hosted_grants import InstallationProofAuthority
from app.security.installation_key import (
    InstallationKeyAuthority,
    InstallationKeyError,
    WindowsCNGInstallationKeyProvider,
)

_ANALYSIS_SEAT_SCOPE = "organization-installation-seat"
_LOCAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MAX_AUTHORITY_REFERENCES = 128

CapabilityLicenseLocalDeliveryRefusal = Literal[
    "activation_package_binding_invalid",
    "activation_package_schema_invalid",
    "activation_refused",
    "capability_license_verification_refused",
    "delivery_refused",
    "durable_store_unavailable",
    "hosted_origin_unavailable",
    "hosted_unavailable",
    "installation_key_mismatch",
    "installation_key_unavailable",
    "local_installation_authority_unavailable",
    "package_binding_mismatch",
    "package_encoding_invalid",
    "proof_challenge_refused",
    "reissue_finalization_refused",
    "reissue_package_binding_invalid",
    "reissue_package_schema_invalid",
    "seat_binding_invalid",
]


@dataclass(frozen=True, slots=True)
class CapabilityLicenseDeliveryReceipt:
    """Nonsecret durable identifiers returned after verified local persistence."""

    reference_id: str
    license_id: str
    issuance_id: str


CapabilityLicenseDeliveryResult = (
    CapabilityLicenseDeliveryReceipt | CapabilityLicenseLocalDeliveryRefusal
)


class CapabilityLicenseLocalDelivery(Protocol):
    def activate(self, *, package: str, seat_id: str) -> CapabilityLicenseDeliveryResult: ...

    def finalize_reissue(
        self, *, package: str, seat_id: str
    ) -> CapabilityLicenseDeliveryResult: ...


TransportFactory = Callable[
    [str, SQLiteCapabilityLicenseDeliveryJournal], CapabilityLicenseTransport
]
ProofAuthorityFactory = Callable[[AuthenticationStore], InstallationProofAuthority]
VerifierFactory = Callable[[], CapabilityLicenseVerifier]


def _production_transport(
    hosted_origin: str,
    journal: SQLiteCapabilityLicenseDeliveryJournal,
) -> CapabilityLicenseTransport:
    return CapabilityLicenseHTTPTransport(hosted_origin, journal=journal)


def _production_proof_authority(
    store: AuthenticationStore,
) -> InstallationProofAuthority:
    return InstallationKeyAuthority(store, WindowsCNGInstallationKeyProvider())


def durable_capability_license_delivery(
    open_store: Callable[[], AuthenticationStore],
    *,
    hosted_origin: str,
    transport_factory: TransportFactory = _production_transport,
    proof_authority_factory: ProofAuthorityFactory = _production_proof_authority,
    verifier_factory: VerifierFactory = CapabilityLicenseVerifier.production,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CapabilityLicenseLocalDelivery:
    """Build the shipping action for activation/reissue package consumption.

    Hosted package coordinates never supply local verification authority. The
    installation tuple must already be represented by current verified local
    activation grants, the key comes from the actual installation-key authority,
    and seat identity is an explicit local selection. Capability, artifact
    family, and major version come from the Brain's analytics runtime selection.
    """

    def deliver(
        *,
        operation: Literal["activate", "finalize"],
        package: str,
        seat_id: str,
    ) -> CapabilityLicenseDeliveryResult:
        if not isinstance(seat_id, str) or _LOCAL_ID.fullmatch(seat_id) is None:
            return "seat_binding_invalid"

        try:
            decoded = (
                decode_activation_package(package)
                if operation == "activate"
                else decode_reissue_package(package)
            )
        except CapabilityLicenseDeliveryRefused as refusal:
            return _known_refusal(refusal.result)

        organization_id = decoded.organization_id
        installation_id = (
            decoded.installation_id
            if operation == "activate"
            else decoded.replacement_installation_id
        )
        capability = (
            decoded.capability if operation == "activate" else ANALYSIS_CAPABILITY
        )
        if capability != ANALYSIS_CAPABILITY:
            return "package_binding_mismatch"

        store = open_store()
        if not isinstance(store, SQLiteAuthenticationStore):
            return "durable_store_unavailable"

        try:
            proof_authority = proof_authority_factory(store)
            key = proof_authority.ensure_ready()
        except InstallationKeyError:
            return "installation_key_unavailable"

        if not _current_local_installation_authority(
            store,
            organization_id=organization_id,
            installation_id=installation_id,
            key=key,
            instant=now(),
        ):
            return "local_installation_authority_unavailable"

        context = _verification_context(
            organization_id=organization_id,
            installation_id=installation_id,
            key=key,
            seat_id=seat_id,
        )

        # Production construction validates the packaged CapabilityLicense trust
        # set before any hosted request is made. Trust defects intentionally
        # propagate as installed-artifact failures rather than user refusals.
        authority = CapabilityLicenseAuthorityService(
            store,
            verifier_factory(),
            verification_source="production",
        )
        journal = SQLiteCapabilityLicenseDeliveryJournal(store.database)
        try:
            transport = transport_factory(hosted_origin, journal)
        except ValueError:
            return "hosted_origin_unavailable"

        try:
            client = CapabilityLicenseDeliveryClient(
                transport,
                proof_authority,
                authority,
            )
            reference = (
                client.activate(package, context=context)
                if operation == "activate"
                else client.finalize_reissue(package, context=context)
            )
        except CapabilityLicenseDeliveryRefused as refusal:
            return _known_refusal(refusal.result)
        except CapabilityLicenseDeliveryUnavailable:
            return "hosted_unavailable"
        except CapabilityLicenseVerificationError:
            return "capability_license_verification_refused"
        except InstallationKeyError:
            return "installation_key_unavailable"
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()

        return _receipt(reference)

    class _Action:
        def activate(
            self, *, package: str, seat_id: str
        ) -> CapabilityLicenseDeliveryResult:
            return deliver(operation="activate", package=package, seat_id=seat_id)

        def finalize_reissue(
            self, *, package: str, seat_id: str
        ) -> CapabilityLicenseDeliveryResult:
            return deliver(operation="finalize", package=package, seat_id=seat_id)

    return _Action()


def _current_local_installation_authority(
    store: SQLiteAuthenticationStore,
    *,
    organization_id: str,
    installation_id: str,
    key: InstallationKeyReference,
    instant: datetime,
) -> bool:
    """Require current local activation grants for exactly this installation tuple."""

    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("CapabilityLicense delivery clock must be timezone-aware")
    grants = store.verified_grants(limit=_MAX_AUTHORITY_REFERENCES + 1)
    if len(grants) > _MAX_AUTHORITY_REFERENCES:
        return False
    selected = []
    for grant_type in ACTIVATION_GRANT_TYPES:
        current = [
            grant
            for grant in grants
            if grant.grant_type == grant_type
            and grant.valid_from <= instant < grant.expires_at
            and grant.organization_id == organization_id
            and grant.installation_id == installation_id
            and grant.installation_key_id == key.installation_key_id
            and grant.installation_key_jkt == key.installation_key_jkt
        ]
        if not current:
            return False
        selected.append(max(current, key=lambda grant: grant.verified_at))
    return len({(grant.issuer, grant.subject) for grant in selected}) == 1


def _verification_context(
    *,
    organization_id: str,
    installation_id: str,
    key: InstallationKeyReference,
    seat_id: str,
) -> CapabilityLicenseVerificationContext:
    subject = (
        f"organization:{organization_id}:installation:{installation_id}:"
        f"seat:{seat_id}:capability:{ANALYSIS_CAPABILITY}"
    )
    return CapabilityLicenseVerificationContext(
        expected_subject=subject,
        expected_organization_id=organization_id,
        expected_installation_id=installation_id,
        expected_installation_key_id=key.installation_key_id,
        expected_installation_key_jkt=key.installation_key_jkt,
        expected_seat_id=seat_id,
        expected_seat_scope=_ANALYSIS_SEAT_SCOPE,
        expected_capability=ANALYSIS_CAPABILITY,
        expected_target_major=ANALYSIS_MAJOR_VERSION,
        expected_artifact_family=ANALYSIS_ARTIFACT_FAMILY,
        requested_update_mode=False,
        expected_fallback_major=None,
    )


def _known_refusal(result: str) -> CapabilityLicenseLocalDeliveryRefusal:
    known = {
        "activation_package_binding_invalid",
        "activation_package_schema_invalid",
        "activation_refused",
        "installation_key_mismatch",
        "package_binding_mismatch",
        "package_encoding_invalid",
        "proof_challenge_refused",
        "reissue_finalization_refused",
        "reissue_package_binding_invalid",
        "reissue_package_schema_invalid",
    }
    return result if result in known else "delivery_refused"  # type: ignore[return-value]


def _receipt(
    reference: VerifiedCapabilityLicenseReference,
) -> CapabilityLicenseDeliveryReceipt:
    return CapabilityLicenseDeliveryReceipt(
        reference_id=reference.reference_id,
        license_id=reference.license_id,
        issuance_id=reference.issuance_id,
    )
