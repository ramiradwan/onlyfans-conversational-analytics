"""Outbound installation-claim and grant-refresh client."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import struct
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Literal, Protocol

import httpx

from app.persistence.auth import (
    AuthenticationStateError,
    AuthenticationStore,
    ClaimSubmissionState,
    InstallationKeyReference,
    OnboardingProgressEvent,
    VerifiedGrantReference,
)
from app.security.grant_verifier import (
    GrantDenialVerificationContext,
    GrantVerificationContext,
    load_pinned_trust_set,
    verify_grant,
    verify_grant_denial,
)
from app.security.grant_types import (
    AGENT_PAIRING_GRANT_TYPES,
    HOSTED_CLAIM_V2_GRANT_TYPES,
    LEGACY_V1_HOSTED_CLAIM_GRANT_TYPES,
    MAX_GRANT_CHARACTERS,
    VerifiedGrantDenial,
)
from app.security.installation_key import InstallationProof
from app.security.runtime_policy import AuthContext, RuntimePolicy


CLAIM_PROFILE_V1 = "urn:bridge-clean:installation-claim:v1"
CLAIM_PROFILE_V2 = "urn:bridge-clean:installation-claim:v2"
CLAIM_PROFILE = CLAIM_PROFILE_V1
BOOTSTRAP_RECOVERY_PROFILE = "urn:bridge-clean:bootstrap-recovery:v2"
PROOF_PROFILE = "urn:bridge-clean:installation-key-proof:v1"
REFRESH_PROFILE = "urn:bridge-clean:grant-refresh:v1"
CREATOR_ASSOCIATION_PROFILE = "urn:bridge-clean:creator-association:v1"
PROOF_AUDIENCE = "urn:bridge-clean:commercial-control-plane:provisioning"
PROGRESS_PROFILE = "urn:bridge-clean:onboarding-progress:v1"
PROGRESS_PROOF_PROFILE = "urn:bridge-clean:provisioning-proof:v1"
PROGRESS_PROOF_AUDIENCE = (
    "urn:bridge-clean:commercial-control-plane:provisioning-v2"
)
PRODUCTION_TRUST_SET = "production/grant-profile-v1/trust-set.json"

_PROOF_DOMAIN = b"BRIDGE-CLEAN-INSTALLATION-PROOF-V1\x00"
_MAX_RESPONSE_BYTES = 65_536
_EXPECTED_MEDIA_TYPE = "application/json"
_UUIDV7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_TIMESTAMP_RE = re.compile(
    r"^(?:(?:[0-9]{2}(?:0[48]|[2468][048]|[13579][26])|(?:[02468][048]|[13579][26])00)-02-29|"
    r"[0-9]{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12][0-9]|3[01])|"
    r"(?:0[469]|11)-(?:0[1-9]|[12][0-9]|30)|02-(?:0[1-9]|1[0-9]|2[0-8])))"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
_AUDIENCES = {
    "installation_grant": "urn:bridge-clean:local-brain:installation",
    "creator_account_binding": "urn:bridge-clean:local-brain:creator-binding",
    "membership_snapshot": "urn:bridge-clean:local-brain:membership",
    "license_entitlement": "urn:bridge-clean:local-brain:license",
}
_GRACE_SECONDS = {
    "installation_grant": 604_800,
    "creator_account_binding": 259_200,
    "membership_snapshot": 259_200,
    "license_entitlement": 604_800,
}
_REFRESH = {
    "installation_grant": (
        "/v1/grants/installation:refresh",
        "installation-grant-refresh",
    ),
    "creator_account_binding": (
        "/v1/grants/creator-binding:refresh",
        "creator-account-binding-refresh",
    ),
    "membership_snapshot": (
        "/v1/grants/membership:refresh",
        "membership-snapshot-refresh",
    ),
    "license_entitlement": (
        "/v1/grants/license:refresh",
        "license-entitlement-refresh",
    ),
}
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})
_ONBOARDING_MILESTONES = frozenset(
    {"installed", "enrolled", "account-bound", "first-capture-ready"}
)


def grant_offline_grace_seconds(grant_type: str) -> int:
    """The offline allowance retained after a grant's signed expiry."""
    try:
        return _GRACE_SECONDS[grant_type]
    except KeyError:
        raise ValueError("Unsupported grant type") from None


class HostedGrantError(RuntimeError):
    """Base failure for hosted grant operations."""


class HostedGrantUnavailable(HostedGrantError):
    """The hosted result is not authoritative."""


class InstallationClaimRefused(HostedGrantError):
    """The installation claim was refused."""


class InstallationClaimReplay(InstallationClaimRefused):
    """The hosted claim store refused a repeated consumption."""


class BootstrapRecoveryRefused(HostedGrantError):
    """An authoritative bootstrap-recovery refusal."""

    def __init__(self, result: str) -> None:
        super().__init__("Bootstrap recovery was refused")
        self.result = result


class GrantVerificationRefused(HostedGrantError):
    """A signed grant did not pass local verification."""

    def __init__(self, result: str) -> None:
        super().__init__("Signed grant verification failed")
        self.result = result


class CreatorAssociationRefused(HostedGrantError):
    """The hosted creator association was refused."""


class CreatorAssociationPending(CreatorAssociationRefused):
    """A different creator association remains pending."""


class _TransportFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    body: bytes = field(repr=False)
    content_type: str


class HostedTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
    ) -> TransportResponse: ...


class InstallationProofAuthority(Protocol):
    def ensure_ready(self) -> InstallationKeyReference: ...

    def sign_challenge(self, challenge: bytes) -> InstallationProof: ...


class HTTPXHostedTransport:
    """HTTPS transport for hosted JSON requests."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
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
    ) -> TransportResponse:
        deadline = monotonic() + self._timeout_seconds
        try:
            with self._client.stream(method, path, json=json_body) as response:
                if monotonic() >= deadline:
                    raise _TransportFailure("Hosted response exceeded deadline")
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise _TransportFailure("Hosted response encoding is unsupported")
                body = bytearray()
                # Encoding is checked before iteration so decompression cannot
                # allocate an unbounded buffer before the JSON size check.
                for chunk in response.iter_raw():
                    if monotonic() >= deadline:
                        raise _TransportFailure("Hosted response exceeded deadline")
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise _TransportFailure("Hosted response exceeds size limit")
                    body.extend(chunk)
                if monotonic() >= deadline:
                    raise _TransportFailure("Hosted response exceeded deadline")
                return TransportResponse(
                    response.status_code,
                    bytes(body),
                    response.headers.get("content-type", ""),
                )
        except Exception:
            raise _TransportFailure("Hosted request failed") from None

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True, slots=True)
class InstallationClaim:
    claim_id: str
    claim_secret: str
    challenge: str
    onboarding_transaction_id: str
    organization_id: str
    installation_id: str
    consume_path: str
    claim_profile: str = CLAIM_PROFILE_V1

    def __post_init__(self) -> None:
        if (
            not _UUIDV7_RE.fullmatch(self.claim_id)
            or not _ID_RE.fullmatch(self.onboarding_transaction_id)
            or not _ID_RE.fullmatch(self.organization_id)
            or (
                self.claim_profile == CLAIM_PROFILE_V2
                and not _UUIDV7_RE.fullmatch(self.installation_id)
            )
            or (
                self.claim_profile == CLAIM_PROFILE_V1
                and not _ID_RE.fullmatch(self.installation_id)
            )
            or self.consume_path
            != f"/v1/installation-claims/{self.claim_id}:consume"
            or self.claim_profile not in {CLAIM_PROFILE_V1, CLAIM_PROFILE_V2}
        ):
            raise ValueError("Installation claim bindings are invalid")
        _decode_32(self.claim_secret)
        _decode_32(self.challenge)


@dataclass(frozen=True, slots=True)
class CreatorAssociationRequest:
    """One locally detected creator account awaiting hosted approval."""

    association_request_id: str
    onboarding_transaction_id: str
    organization_id: str
    installation_id: str
    creator_account_id: str

    def __post_init__(self) -> None:
        if (
            not _UUIDV7_RE.fullmatch(self.association_request_id)
            or not all(
                _ID_RE.fullmatch(value)
                for value in (
                    self.onboarding_transaction_id,
                    self.organization_id,
                    self.installation_id,
                    self.creator_account_id,
                )
            )
        ):
            raise ValueError("Creator association bindings are invalid")


@dataclass(frozen=True, slots=True)
class CreatorAssociationStatus:
    """The hosted state for one creator association request."""

    updated_at: str


@dataclass(frozen=True, slots=True)
class DeviceMetadata:
    platform: Literal["windows", "macos", "linux"]
    product_version: str
    display_name: str

    def __post_init__(self) -> None:
        if self.platform not in {"windows", "macos", "linux"}:
            raise ValueError("Device platform is invalid")
        if not _VERSION_RE.fullmatch(self.product_version):
            raise ValueError("Product version is invalid")
        if (
            not 1 <= len(self.display_name) <= 128
            or any(ord(character) < 32 or ord(character) == 127 for character in self.display_name)
        ):
            raise ValueError("Device display name is invalid")


@dataclass(frozen=True, slots=True)
class ClaimConsumption:
    grant_reference_ids: tuple[str, ...]
    policy: RuntimePolicy | None
    enrolled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BootstrapRecovery:
    grant_reference_ids: tuple[str, ...]
    enrolled_at: datetime
    recovered_at: datetime
    selected_profiles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GrantRefresh:
    state: Literal["updated", "unknown", "revoked"]
    grant_reference_ids: tuple[str, ...]
    policy: RuntimePolicy | None


ProgressDelivery = Literal["delivered", "retry", "refused"]
GrantCommitGuard = Callable[[], AbstractContextManager[bool]]


class HostedGrantClient:
    """Consumes installation claims and refreshes verified local grants."""

    def __init__(
        self,
        transport: HostedTransport,
        installation_key: InstallationProofAuthority,
        store: AuthenticationStore,
        *,
        clock: Callable[[], datetime] | None = None,
        trust_set: Mapping[str, object] | None = None,
    ) -> None:
        self._transport = transport
        self._installation_key = installation_key
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trust_set = (
            load_pinned_trust_set(PRODUCTION_TRUST_SET)
            if trust_set is None
            else trust_set
        )

    def close(self) -> None:
        """Release the hosted connection pool after outstanding work has stopped."""
        close = getattr(self._transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                raise HostedGrantUnavailable("Hosted transport could not close") from None

    def consume_claim(
        self,
        claim: InstallationClaim,
        device: DeviceMetadata,
        *,
        identity: AuthContext | None = None,
    ) -> ClaimConsumption:
        key = self._installation_key.ensure_ready()
        public_jwk = _public_jwk(key)
        request_body: dict[str, object] = {
            "profile": claim.claim_profile,
            "claim_secret": claim.claim_secret,
            "onboarding_transaction_id": claim.onboarding_transaction_id,
            "organization_id": claim.organization_id,
            "installation_id": claim.installation_id,
            "installation_key": {
                "alg": "ES256",
                "kid": key.installation_key_id,
                "jwk": public_jwk,
            },
            "device": {
                "platform": device.platform,
                "product_version": device.product_version,
                "display_name": device.display_name,
            },
        }
        envelope = {
            "request": request_body,
            "proof": self._proof(
                challenge=claim.challenge,
                request_body=request_body,
                path=claim.consume_path,
                purpose="installation-claim-consume",
                installation_id=claim.installation_id,
                key=key,
            ),
        }
        response = self._request(claim.consume_path, envelope)
        if response.status_code == 409:
            raise InstallationClaimReplay("Installation claim replay was refused")
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise HostedGrantUnavailable("Hosted claim result is unavailable")
        if response.status_code != 200:
            raise InstallationClaimRefused("Installation claim was refused")
        document = _response_object(response)
        grant_types = (
            HOSTED_CLAIM_V2_GRANT_TYPES
            if claim.claim_profile == CLAIM_PROFILE_V2
            else LEGACY_V1_HOSTED_CLAIM_GRANT_TYPES
        )
        tokens, consumed_at = self._validated_claim_response(
            document, claim, key, grant_types=grant_types
        )
        references = self._verify_bundle(
            tokens,
            grant_types=grant_types,
            organization_id=claim.organization_id,
            installation_id=claim.installation_id,
            key=key,
            identity=identity,
        )
        self._store.record_verified_grants(references)
        reference_ids = tuple(reference.reference_id for reference in references)
        policy = (
            None
            if identity is None
            else self._store.build_runtime_policy_from_grants(identity, reference_ids)
        )
        return ClaimConsumption(reference_ids, policy, consumed_at)

    def recover_bootstrap_v2(
        self,
        *,
        claim_id: str,
        recovery_request_id: str,
        supported_profiles: tuple[str, ...] = (
            "urn:bridge-clean:grant-profile:v1",
            "urn:bridge-clean:onboarding-progress:v1",
        ),
    ) -> BootstrapRecovery:
        """Recover bootstrap identity authority for one possibly committed v2 enrollment."""

        submission = self._store.claim_submission(claim_id)
        if (
            submission is None
            or submission.claim_profile != CLAIM_PROFILE_V2
            or submission.state is ClaimSubmissionState.REFUSED
        ):
            raise BootstrapRecoveryRefused("enrollment_not_found")
        if not _UUIDV7_RE.fullmatch(recovery_request_id):
            raise ValueError("Bootstrap recovery request identifier is invalid")
        if (
            not supported_profiles
            or len(supported_profiles) > 16
            or len(set(supported_profiles)) != len(supported_profiles)
            or not all(isinstance(value, str) and value.startswith("urn:bridge-clean:") for value in supported_profiles)
        ):
            raise ValueError("Bootstrap recovery supported profiles are invalid")
        key = self._installation_key.ensure_ready()
        if key.installation_key_id == "" or key.installation_key_jkt == "":
            raise AuthenticationStateError("Installation key is unavailable")
        challenge_path = f"/v1/installations/{submission.installation_id}/proof-challenges"
        challenge_response = self._request(
            challenge_path,
            {"profile": PROGRESS_PROOF_PROFILE, "purpose": "bootstrap-recovery"},
        )
        if _retryable(challenge_response.status_code):
            raise HostedGrantUnavailable("Bootstrap recovery proof challenge is unavailable")
        if challenge_response.status_code != 201:
            raise BootstrapRecoveryRefused("proof_challenge_refused")
        challenge = self._validated_challenge(
            _response_object(challenge_response),
            installation_id=submission.installation_id,
            purpose="bootstrap-recovery",
            profile=PROGRESS_PROOF_PROFILE,
            audience=PROGRESS_PROOF_AUDIENCE,
        )
        request_body: dict[str, object] = {
            "profile": BOOTSTRAP_RECOVERY_PROFILE,
            "recovery_request_id": recovery_request_id,
            "claim_id": submission.claim_id,
            "onboarding_transaction_id": submission.onboarding_transaction_id,
            "organization_id": submission.organization_id,
            "installation_id": submission.installation_id,
            "installation_key_id": key.installation_key_id,
            "supported_profiles": list(supported_profiles),
        }
        path = f"/v1/installations/{submission.installation_id}/bootstrap:recover"
        response = self._request(
            path,
            {
                "request": request_body,
                "proof": self._proof(
                    challenge=challenge,
                    request_body=request_body,
                    path=path,
                    purpose="bootstrap-recovery",
                    installation_id=submission.installation_id,
                    key=key,
                    audience=PROGRESS_PROOF_AUDIENCE,
                ),
            },
        )
        if _retryable(response.status_code):
            raise HostedGrantUnavailable("Bootstrap recovery is temporarily unavailable")
        refusal = {
            401: "registered_key_mismatch",
            403: "installation_binding_mismatch",
            404: "enrollment_not_found",
            409: "proof_challenge_replayed",
        }.get(response.status_code)
        if refusal is not None:
            raise BootstrapRecoveryRefused(refusal)
        if response.status_code != 200:
            raise BootstrapRecoveryRefused("recovery_refused")
        document = _response_object(response)
        tokens, enrolled_at, recovered_at, selected = self._validated_bootstrap_recovery_response(
            document,
            submission=submission,
            recovery_request_id=recovery_request_id,
            key=key,
            supported_profiles=supported_profiles,
        )
        references = self._verify_bundle(
            tokens,
            grant_types=HOSTED_CLAIM_V2_GRANT_TYPES,
            organization_id=submission.organization_id,
            installation_id=submission.installation_id,
            key=key,
            identity=None,
        )
        self._store.record_verified_grants(references)
        self._store.resolve_claim_submission(
            claim_id, outcome=None, resolved_at=recovered_at, enrolled_at=enrolled_at
        )
        return BootstrapRecovery(
            tuple(reference.reference_id for reference in references),
            enrolled_at,
            recovered_at,
            selected,
        )

    def runtime_policy(
        self,
        identity: AuthContext,
        grant_reference_ids: tuple[str, ...],
    ) -> RuntimePolicy:
        return self._store.build_runtime_policy_from_grants(
            identity, grant_reference_ids
        )

    def report_onboarding_progress(
        self, event: OnboardingProgressEvent
    ) -> ProgressDelivery:
        """Push one closed event with provisioning-v2 installation-key proof."""

        if event.milestone not in _ONBOARDING_MILESTONES:
            raise ValueError("Onboarding progress milestone is invalid")
        key = self._installation_key.ensure_ready()
        if not self._progress_key_is_bound(event, key):
            # `installed` is durably observed before enrollment can install the
            # verified grant. Local not-ready state must remain retryable.
            return "retry"
        challenge_path = (
            f"/v1/installations/{event.installation_id}/proof-challenges"
        )
        try:
            challenge_response = self._request(
                challenge_path,
                {
                    "profile": PROGRESS_PROOF_PROFILE,
                    "purpose": "onboarding-progress-report",
                },
            )
        except HostedGrantUnavailable:
            return "retry"
        if _retryable(challenge_response.status_code):
            return "retry"
        if 400 <= challenge_response.status_code < 500:
            return "refused"
        if challenge_response.status_code != 201:
            return "retry"
        try:
            challenge = self._validated_progress_challenge(
                _response_object(challenge_response),
                installation_id=event.installation_id,
            )
        except (HostedGrantUnavailable, ValueError):
            return "retry"

        path = f"/v1/installations/{event.installation_id}/onboarding-progress"
        request_body = self._progress_request(event)
        envelope = {
            "request": request_body,
            "proof": self._proof(
                challenge=challenge,
                request_body=request_body,
                path=path,
                purpose="onboarding-progress-report",
                installation_id=event.installation_id,
                key=key,
                audience=PROGRESS_PROOF_AUDIENCE,
            ),
        }
        try:
            response = self._request(path, envelope)
        except HostedGrantUnavailable:
            return "retry"
        if _retryable(response.status_code):
            return "retry"
        if 400 <= response.status_code < 500:
            return "refused"
        if response.status_code != 200:
            return "retry"
        try:
            self._validated_progress_response(_response_object(response), event)
        except HostedGrantUnavailable:
            return "retry"
        return "delivered"

    def request_creator_association(
        self, association: CreatorAssociationRequest
    ) -> CreatorAssociationStatus:
        """Request hosted approval for one locally detected creator account."""

        key = self._installation_key.ensure_ready()
        request_body: dict[str, object] = {
            "profile": CREATOR_ASSOCIATION_PROFILE,
            "association_request_id": association.association_request_id,
            "onboarding_transaction_id": association.onboarding_transaction_id,
            "organization_id": association.organization_id,
            "installation_id": association.installation_id,
            "creator_account_id": association.creator_account_id,
            "installation_key_id": key.installation_key_id,
        }
        path = (
            f"/v1/installations/{association.installation_id}/creator-associations"
        )
        challenge = self._proof_challenge(
            installation_id=association.installation_id,
            purpose="creator-association-request",
            account_id=association.creator_account_id,
        )
        response = self._request(
            path,
            {
                "request": request_body,
                "proof": self._proof(
                    challenge=challenge,
                    request_body=request_body,
                    path=path,
                    purpose="creator-association-request",
                    installation_id=association.installation_id,
                    account_id=association.creator_account_id,
                    key=key,
                    audience=PROGRESS_PROOF_AUDIENCE,
                ),
            },
        )
        if response.status_code == 409:
            raise CreatorAssociationPending(
                "Creator association request is already pending"
            )
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise HostedGrantUnavailable("Hosted creator association is unavailable")
        if response.status_code != 202:
            raise CreatorAssociationRefused("Creator association was refused")
        return self._validated_association_response(
            _response_object(response), association, status="pending", binding=False
        )

    def acquire_creator_account_binding(
        self,
        association: CreatorAssociationRequest,
        *,
        membership_reference_id: str,
    ) -> VerifiedGrantReference:
        """Verify and return an approved creator-account binding."""

        key = self._installation_key.ensure_ready()
        membership = self._association_membership(
            association, membership_reference_id, key
        )
        path = (
            f"/v1/installations/{association.installation_id}/creator-associations/"
            f"{association.association_request_id}"
        )
        request_body: dict[str, object] = {
            "profile": CREATOR_ASSOCIATION_PROFILE,
            "association_request_id": association.association_request_id,
            "installation_id": association.installation_id,
        }
        challenge = self._proof_challenge(
            installation_id=association.installation_id,
            purpose="creator-association-status",
            account_id=association.creator_account_id,
        )
        response = self._request(
            path,
            {
                "request": request_body,
                "proof": self._proof(
                    challenge=challenge,
                    request_body=request_body,
                    path=path,
                    purpose="creator-association-status",
                    installation_id=association.installation_id,
                    account_id=association.creator_account_id,
                    key=key,
                    audience=PROGRESS_PROOF_AUDIENCE,
                ),
            },
        )
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise HostedGrantUnavailable("Hosted creator association is unavailable")
        if response.status_code != 200:
            raise CreatorAssociationRefused("Creator association was refused")
        document = _response_object(response)
        self._validated_association_response(
            document, association, status="approved", binding=True
        )
        token = document["creator_account_binding"]
        if not isinstance(token, str):
            raise HostedGrantUnavailable("Hosted creator association is invalid")
        payload = _untrusted_payload(token)
        reference = self._verified_reference(
            token,
            payload,
            grant_type="creator_account_binding",
            organization_id=association.organization_id,
            installation_id=association.installation_id,
            key=key,
            external_issuer=membership.issuer,
            external_subject=membership.subject,
            requested_account_ids=(),
        )
        if reference.creator_account_id != association.creator_account_id:
            raise GrantVerificationRefused("creator_account_mismatch")
        return reference

    def refresh_grant(
        self,
        identity: AuthContext,
        grant_reference_ids: tuple[str, ...],
        reference_id: str,
        *,
        commit_guard: GrantCommitGuard | None = None,
    ) -> GrantRefresh:
        if reference_id not in grant_reference_ids:
            raise AuthenticationStateError("Verified grant refresh reference is unavailable")
        current = self._store.verified_grant(reference_id)
        refreshed = self.refresh_reference(reference_id, commit_guard=commit_guard)
        if refreshed.state == "updated":
            reference_ids = tuple(
                refreshed.grant_reference_ids[0] if value == reference_id else value
                for value in grant_reference_ids
            )
        elif refreshed.state == "revoked":
            active_ids = {grant.reference_id for grant in self._store.verified_grants()}
            reference_ids = tuple(value for value in grant_reference_ids if value in active_ids)
        else:
            reference_ids = grant_reference_ids
        policy = None
        if reference_ids and (
            refreshed.state != "revoked"
            or (current is not None and current.grant_type == "license_entitlement")
        ):
            try:
                policy = self.runtime_policy(identity, reference_ids)
            except AuthenticationStateError:
                pass
        return GrantRefresh(refreshed.state, reference_ids, policy)

    def refresh_reference(
        self,
        reference_id: str,
        *,
        commit_guard: GrantCommitGuard | None = None,
    ) -> GrantRefresh:
        """Refresh installation-bound authority without requiring runtime access."""
        current = self._store.verified_grant(reference_id)
        now = self._now()
        if (
            current is None
            or reference_id not in {grant.reference_id for grant in self._store.verified_grants()}
            or not current.valid_from <= now < current.expires_at
        ):
            raise AuthenticationStateError("Verified grant refresh reference is unavailable")
        key = self._installation_key.ensure_ready()
        if (
            current.installation_key_id != key.installation_key_id
            or current.installation_key_jkt != key.installation_key_jkt
        ):
            raise AuthenticationStateError("Verified grant installation binding is invalid")
        path, purpose = _REFRESH[current.grant_type]
        account_id = current.creator_account_id or ""
        challenge_path = f"/v1/installations/{current.installation_id}/proof-challenges"
        challenge_request: dict[str, object] = {
            "profile": PROOF_PROFILE,
            "purpose": purpose,
            "account_id": account_id,
        }
        denial: VerifiedGrantDenial | None = None
        try:
            challenge_response = self._request(challenge_path, challenge_request)
            if challenge_response.status_code != 201:
                raise HostedGrantUnavailable("Hosted refresh challenge is unavailable")
            challenge = self._validated_challenge(
                _response_object(challenge_response),
                installation_id=current.installation_id,
                purpose=purpose,
            )
            request_body = self._refresh_request(current)
            envelope = {
                "request": request_body,
                "proof": self._proof(
                    challenge=challenge,
                    request_body=request_body,
                    path=path,
                    purpose=purpose,
                    installation_id=current.installation_id,
                    account_id=account_id,
                    key=key,
                ),
            }
            response = self._request(path, envelope)
            if response.status_code == 403:
                denial = self._verified_denial(response, current)
            elif response.status_code != 200:
                raise HostedGrantUnavailable("Hosted refresh result is unavailable")
            else:
                document = _response_object(response)
                token = self._validated_refresh_response(document, current)
                payload = _untrusted_payload(token)
                replacement = self._verified_reference(
                    token,
                    payload,
                    grant_type=current.grant_type,
                    organization_id=current.organization_id or "",
                    installation_id=current.installation_id,
                    key=key,
                    external_issuer=current.issuer,
                    external_subject=current.subject,
                    requested_account_ids=(),
                )
                immutable_fields = {
                    "installation_grant": (),
                    "creator_account_binding": (
                        "creator_account_id", "approval_id", "approval_revision",
                    ),
                    "membership_snapshot": ("membership_id",),
                    "license_entitlement": ("entitlement_id", "product_id"),
                }[current.grant_type]
                if replacement.grant_identifier == current.grant_identifier or any(
                    getattr(replacement, member) != getattr(current, member)
                    for member in immutable_fields
                ):
                    raise GrantVerificationRefused("refresh_context_mismatch")
        except (HostedGrantUnavailable, GrantVerificationRefused, ValueError, KeyError):
            return GrantRefresh("unknown", (reference_id,), None)

        with (nullcontext(True) if commit_guard is None else commit_guard()) as allowed:
            if allowed is not True:
                return GrantRefresh("unknown", (reference_id,), None)
            if denial is not None:
                applied = self._store.apply_hosted_grant_denial(current, denial)
                if applied == "stale":
                    # A delayed answer cannot select or withdraw newer authority.
                    return GrantRefresh("unknown", (reference_id,), None)
                return GrantRefresh("revoked", (), None)
            self._store.replace_verified_grant(reference_id, replacement, expected=current)
        return GrantRefresh("updated", (replacement.reference_id,), None)

    def _verified_denial(
        self, response: TransportResponse, current: VerifiedGrantReference
    ) -> VerifiedGrantDenial:
        document = _response_object(response)
        token = document.get("denial_jws")
        if set(document) != {"denial_jws"} or not isinstance(token, str):
            raise HostedGrantUnavailable("Hosted denial is invalid")
        subject = _expected_subject(
            current.grant_type,
            {"creator_account_id": current.creator_account_id},
            organization_id=current.organization_id or "",
            installation_id=current.installation_id,
            external_issuer=current.issuer,
            external_subject=current.subject,
        )
        result = verify_grant_denial(
            token,
            trust_set=self._trust_set,
            context=GrantDenialVerificationContext(
                expected_grant_type=current.grant_type,
                expected_audience=_AUDIENCES[current.grant_type],
                expected_subject=subject,
                expected_revoked_jti=current.grant_identifier,
                verifier_time=int(self._now().timestamp()),
            ),
        )
        if not result.valid or result.denial is None:
            raise GrantVerificationRefused(result.result)
        return result.denial

    def _request(
        self, path: str, json_body: Mapping[str, object]
    ) -> TransportResponse:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise HostedGrantUnavailable("Hosted request path is invalid")
        try:
            response = self._transport.request(
                "POST", path, json_body=json_body
            )
        except Exception:
            raise HostedGrantUnavailable("Hosted request is unavailable") from None
        if (
            not isinstance(response, TransportResponse)
            or not isinstance(response.status_code, int)
            or isinstance(response.status_code, bool)
            or not 100 <= response.status_code <= 599
            or not isinstance(response.body, bytes)
            or len(response.body) > _MAX_RESPONSE_BYTES
            or not isinstance(response.content_type, str)
            or _media_type(response.content_type) != _EXPECTED_MEDIA_TYPE
        ):
            raise HostedGrantUnavailable("Hosted response is unavailable")
        return response

    def _proof_challenge(
        self,
        *,
        installation_id: str,
        purpose: Literal["creator-association-request", "creator-association-status"],
        account_id: str,
    ) -> str:
        response = self._request(
            f"/v1/installations/{installation_id}/proof-challenges",
            {
                "profile": PROGRESS_PROOF_PROFILE,
                "purpose": purpose,
                "account_id": account_id,
            },
        )
        if response.status_code != 201:
            raise HostedGrantUnavailable("Hosted proof challenge is unavailable")
        return self._validated_challenge(
            _response_object(response),
            installation_id=installation_id,
            purpose=purpose,
            profile=PROGRESS_PROOF_PROFILE,
            audience=PROGRESS_PROOF_AUDIENCE,
        )

    def _association_membership(
        self,
        association: CreatorAssociationRequest,
        membership_reference_id: str,
        key: InstallationKeyReference,
    ) -> VerifiedGrantReference:
        membership = self._store.verified_grant(membership_reference_id)
        if (
            membership is None
            or membership.grant_type != "membership_snapshot"
            or membership.organization_id != association.organization_id
            or membership.installation_id != association.installation_id
            or membership.installation_key_id != key.installation_key_id
            or membership.installation_key_jkt != key.installation_key_jkt
        ):
            raise AuthenticationStateError(
                "Creator association membership reference is unavailable"
            )
        return membership

    @staticmethod
    def _validated_association_response(
        document: Mapping[str, object],
        association: CreatorAssociationRequest,
        *,
        status: Literal["pending", "approved"],
        binding: bool,
    ) -> CreatorAssociationStatus:
        updated_at = document.get("updated_at")
        expected = {
            "profile",
            "association_request_id",
            "organization_id",
            "installation_id",
            "creator_account_id",
            "status",
            "updated_at",
            "creator_account_binding",
        }
        if (
            set(document) != expected
            or document.get("profile") != CREATOR_ASSOCIATION_PROFILE
            or document.get("association_request_id")
            != association.association_request_id
            or document.get("organization_id") != association.organization_id
            or document.get("installation_id") != association.installation_id
            or document.get("creator_account_id") != association.creator_account_id
            or document.get("status") != status
            or not isinstance(updated_at, str)
            or (
                document.get("creator_account_binding") is not None
                if not binding
                else not isinstance(document.get("creator_account_binding"), str)
            )
        ):
            raise HostedGrantUnavailable("Hosted creator association is invalid")
        return CreatorAssociationStatus(updated_at)

    def _proof(
        self,
        *,
        challenge: str,
        request_body: Mapping[str, object],
        path: str,
        purpose: str,
        installation_id: str,
        key: InstallationKeyReference,
        account_id: str = "",
        audience: str = PROOF_AUDIENCE,
    ) -> dict[str, str]:
        fields = (
            _decode_32(challenge),
            b"POST",
            path.encode("ascii"),
            hashlib.sha256(_canonical_json(request_body)).digest(),
            purpose.encode("ascii"),
            installation_id.encode("utf-8"),
            account_id.encode("utf-8"),
            key.installation_key_id.encode("utf-8"),
            audience.encode("ascii"),
        )
        canonical = bytearray(_PROOF_DOMAIN)
        for value in fields:
            canonical += struct.pack("!I", len(value)) + value
        proof = self._installation_key.sign_challenge(bytes(canonical))
        if (
            proof.installation_key_id != key.installation_key_id
            or proof.algorithm != "ES256"
            or len(proof.signature) != 64
        ):
            raise HostedGrantUnavailable("Installation proof is unavailable")
        return {
            "challenge": challenge,
            "key_id": key.installation_key_id,
            "signature": _b64url(proof.signature),
        }

    def _progress_key_is_bound(
        self, event: OnboardingProgressEvent, key: InstallationKeyReference
    ) -> bool:
        """Require one verified installation grant for the event/key tuple."""

        return any(
            grant.grant_type == "installation_grant"
            and grant.installation_id == event.installation_id
            and grant.organization_id == event.organization_id
            and grant.installation_key_id == key.installation_key_id
            and grant.installation_key_jkt == key.installation_key_jkt
            for grant in self._store.verified_grants()
        )

    @staticmethod
    def _progress_request(event: OnboardingProgressEvent) -> dict[str, object]:
        return {
            "profile": PROGRESS_PROFILE,
            "event_id": event.event_id,
            "milestone": event.milestone,
            "occurred_at": _contract_timestamp(event.occurred_at),
            "onboarding_transaction_id": event.onboarding_transaction_id,
            "organization_id": event.organization_id,
            "installation_id": event.installation_id,
            "correlation_id": event.correlation_id,
        }

    @staticmethod
    def _validated_progress_challenge(
        document: Mapping[str, object], *, installation_id: str
    ) -> str:
        challenge = document.get("challenge")
        issued_at = document.get("issued_at")
        expires_at = document.get("expires_at")
        expected = {
            "profile",
            "purpose",
            "installation_id",
            "challenge",
            "audience",
            "issued_at",
            "expires_at",
        }
        if (
            set(document) != expected
            or document.get("profile") != PROGRESS_PROOF_PROFILE
            or document.get("purpose") != "onboarding-progress-report"
            or document.get("installation_id") != installation_id
            or document.get("audience") != PROGRESS_PROOF_AUDIENCE
            or not isinstance(challenge, str)
            or not isinstance(issued_at, str)
            or _TIMESTAMP_RE.fullmatch(issued_at) is None
            or not isinstance(expires_at, str)
            or _TIMESTAMP_RE.fullmatch(expires_at) is None
        ):
            raise HostedGrantUnavailable("Hosted progress challenge is invalid")
        _decode_32(challenge)
        return challenge

    @staticmethod
    def _validated_progress_response(
        document: Mapping[str, object], event: OnboardingProgressEvent
    ) -> None:
        recorded_at = document.get("recorded_at")
        if (
            set(document)
            != {"profile", "event_id", "milestone", "status", "recorded_at"}
            or document.get("profile") != PROGRESS_PROFILE
            or document.get("event_id") != event.event_id
            or document.get("milestone") != event.milestone
            or document.get("status") not in {"recorded", "duplicate"}
            or not isinstance(recorded_at, str)
            or _TIMESTAMP_RE.fullmatch(recorded_at) is None
        ):
            raise HostedGrantUnavailable("Hosted progress response is invalid")

    def _validated_claim_response(
        self,
        document: Mapping[str, object],
        claim: InstallationClaim,
        key: InstallationKeyReference,
        *,
        grant_types: tuple[str, ...],
    ) -> tuple[dict[str, str], datetime]:
        expected = {
            "profile",
            "status",
            "claim_id",
            "onboarding_transaction_id",
            "organization_id",
            "installation_id",
            "installation_key_id",
            "installation_key_jkt",
            "consumed_at",
            "grants",
            "bootstrap_config_version",
        }
        if set(document) != expected or any(
            (
                document.get("profile") != claim.claim_profile,
                document.get("status") != "consumed",
                document.get("claim_id") != claim.claim_id,
                document.get("onboarding_transaction_id")
                != claim.onboarding_transaction_id,
                document.get("organization_id") != claim.organization_id,
                document.get("installation_id") != claim.installation_id,
                document.get("installation_key_id") != key.installation_key_id,
                document.get("installation_key_jkt") != key.installation_key_jkt,
            )
        ):
            raise HostedGrantUnavailable("Hosted claim response is invalid")
        grants = document.get("grants")
        if (
            not isinstance(grants, dict)
            or set(grants) != set(grant_types)
            or not all(isinstance(grants[name], str) for name in grant_types)
            or not isinstance(document.get("consumed_at"), str)
            or _TIMESTAMP_RE.fullmatch(str(document.get("consumed_at"))) is None
            or not isinstance(document.get("bootstrap_config_version"), str)
        ):
            raise HostedGrantUnavailable("Hosted claim response is invalid")
        consumed_at = _parse_contract_timestamp(str(document["consumed_at"]))
        return ({name: grants[name] for name in grant_types}, consumed_at)

    def _verify_bundle(
        self,
        tokens: Mapping[str, str],
        *,
        grant_types: tuple[str, ...],
        organization_id: str,
        installation_id: str,
        key: InstallationKeyReference,
        identity: AuthContext | None,
    ) -> tuple[VerifiedGrantReference, ...]:
        payloads = {
            grant_type: _untrusted_payload(tokens[grant_type])
            for grant_type in grant_types
        }
        membership = payloads["membership_snapshot"]
        external_issuer = _required_string(membership, "ciam_issuer")
        external_subject = _required_string(membership, "ciam_subject")
        requested_accounts = (
            () if identity is None else (identity.creator_account_id,)
        )
        references = tuple(
            self._verified_reference(
                tokens[grant_type],
                payloads[grant_type],
                grant_type=grant_type,
                organization_id=organization_id,
                installation_id=installation_id,
                key=key,
                external_issuer=external_issuer,
                external_subject=external_subject,
                requested_account_ids=(
                    requested_accounts
                    if grant_type == "membership_snapshot"
                    else ()
                ),
            )
            for grant_type in grant_types
        )
        return references

    def _validated_bootstrap_recovery_response(
        self,
        document: Mapping[str, object],
        *,
        submission: object,
        recovery_request_id: str,
        key: InstallationKeyReference,
        supported_profiles: tuple[str, ...],
    ) -> tuple[dict[str, str], datetime, datetime, tuple[str, ...]]:
        expected = {
            "profile", "status", "recovery_request_id", "claim_id",
            "onboarding_transaction_id", "organization_id", "installation_id",
            "installation_key_id", "installation_key_jkt", "enrolled_at",
            "recovered_at", "grants", "bootstrap_config_version", "selected_profiles",
        }
        if set(document) != expected:
            raise HostedGrantUnavailable("Bootstrap recovery response is invalid")
        # ClaimSubmission is kept structurally typed here to avoid granting it authority.
        if (
            document.get("profile") != BOOTSTRAP_RECOVERY_PROFILE
            or document.get("status") != "recovered"
            or document.get("recovery_request_id") != recovery_request_id
            or document.get("claim_id") != getattr(submission, "claim_id")
            or document.get("onboarding_transaction_id") != getattr(submission, "onboarding_transaction_id")
            or document.get("organization_id") != getattr(submission, "organization_id")
            or document.get("installation_id") != getattr(submission, "installation_id")
            or document.get("installation_key_id") != key.installation_key_id
            or document.get("installation_key_jkt") != key.installation_key_jkt
        ):
            raise HostedGrantUnavailable("Bootstrap recovery response bindings are invalid")
        enrolled_text = document.get("enrolled_at")
        recovered_text = document.get("recovered_at")
        selected_value = document.get("selected_profiles")
        grants = document.get("grants")
        if (
            not isinstance(enrolled_text, str)
            or not isinstance(recovered_text, str)
            or not isinstance(document.get("bootstrap_config_version"), str)
            or not isinstance(selected_value, list)
            or not selected_value
            or len(selected_value) > 16
            or len(set(selected_value)) != len(selected_value)
            or not all(isinstance(value, str) for value in selected_value)
            or not set(selected_value).issubset(supported_profiles)
            or not isinstance(grants, dict)
            or set(grants) != set(HOSTED_CLAIM_V2_GRANT_TYPES)
            or not all(isinstance(grants[name], str) for name in HOSTED_CLAIM_V2_GRANT_TYPES)
        ):
            raise HostedGrantUnavailable("Bootstrap recovery response is invalid")
        enrolled_at = _parse_contract_timestamp(enrolled_text)
        recovered_at = _parse_contract_timestamp(recovered_text)
        recorded_enrolled = getattr(submission, "enrolled_at")
        if recorded_enrolled is not None and recorded_enrolled != enrolled_at:
            raise HostedGrantUnavailable("Bootstrap recovery enrollment time changed")
        return (
            {name: grants[name] for name in HOSTED_CLAIM_V2_GRANT_TYPES},
            enrolled_at,
            recovered_at,
            tuple(selected_value),
        )

    def _verified_reference(
        self,
        token: str,
        payload: Mapping[str, object],
        *,
        grant_type: str,
        organization_id: str,
        installation_id: str,
        key: InstallationKeyReference,
        external_issuer: str,
        external_subject: str,
        requested_account_ids: tuple[str, ...],
    ) -> VerifiedGrantReference:
        subject = _expected_subject(
            grant_type,
            payload,
            organization_id=organization_id,
            installation_id=installation_id,
            external_issuer=external_issuer,
            external_subject=external_subject,
        )
        if grant_type == "membership_snapshot" and (
            payload.get("ciam_issuer") != external_issuer
            or payload.get("ciam_subject") != external_subject
        ):
            raise GrantVerificationRefused("subject_mismatch")
        context = GrantVerificationContext(
            expected_grant_type=grant_type,
            expected_audience=_AUDIENCES[grant_type],
            expected_organization_id=organization_id,
            expected_installation_id=installation_id,
            expected_installation_key_id=key.installation_key_id,
            expected_installation_key_jkt=key.installation_key_jkt,
            expected_subject=subject,
            verifier_time=int(self._now().timestamp()),
            requested_operation=(
                "runtime:existing-data" if requested_account_ids else ""
            ),
            requested_creator_account_ids=requested_account_ids,
        )
        result = verify_grant(token, context=context, trust_set=self._trust_set)
        if not result.valid:
            raise GrantVerificationRefused(result.result)
        if not token.isascii() or len(token) > MAX_GRANT_CHARACTERS:
            raise GrantVerificationRefused("invalid_compact_jws")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        # Only the two pairing grant types travel in a companion
        # `session.authorization`, so only those are retained; every other
        # grant type keeps the digest alone.
        retained = token if grant_type in AGENT_PAIRING_GRANT_TYPES else None
        valid_from = datetime.fromtimestamp(_required_int(payload, "nbf"), timezone.utc)
        signed_expiry = datetime.fromtimestamp(_required_int(payload, "exp"), timezone.utc)
        creator_account_id = (
            _required_string(payload, "creator_account_id")
            if grant_type == "creator_account_binding"
            else None
        )
        allowed_accounts = (
            _required_string_tuple(payload, "allowed_creator_account_ids")
            if grant_type == "membership_snapshot"
            else None
        )
        membership_roles = (
            _required_string_tuple(payload, "roles")
            if grant_type == "membership_snapshot"
            else None
        )
        return VerifiedGrantReference(
            reference_id=f"vg1.{_b64url(digest[:16])}",
            grant_identifier=_required_string(payload, "jti"),
            grant_type=grant_type,
            grant_digest=digest.hex(),
            issuer=external_issuer,
            subject=external_subject,
            installation_id=installation_id,
            creator_account_id=creator_account_id,
            valid_from=valid_from,
            expires_at=signed_expiry
            + timedelta(seconds=_GRACE_SECONDS[grant_type]),
            verified_at=self._now(),
            organization_id=organization_id,
            installation_key_id=key.installation_key_id,
            installation_key_jkt=key.installation_key_jkt,
            membership_id=(
                _required_string(payload, "membership_id")
                if grant_type == "membership_snapshot"
                else None
            ),
            approval_id=(
                _required_string(payload, "approval_id")
                if grant_type == "creator_account_binding"
                else None
            ),
            approval_revision=(
                _required_int(payload, "approval_revision")
                if grant_type == "creator_account_binding"
                else None
            ),
            entitlement_id=(
                _required_string(payload, "entitlement_id")
                if grant_type == "license_entitlement"
                else None
            ),
            product_id=(
                _required_string(payload, "product_id")
                if grant_type == "license_entitlement"
                else None
            ),
            allowed_creator_account_ids=allowed_accounts,
            membership_roles=membership_roles,
            compact_jws=retained,
        )

    @staticmethod
    def _validated_challenge(
        document: Mapping[str, object],
        *,
        installation_id: str,
        purpose: str,
        profile: str = PROOF_PROFILE,
        audience: str = PROOF_AUDIENCE,
    ) -> str:
        challenge = document.get("challenge")
        if (
            set(document)
            != {
                "profile",
                "purpose",
                "installation_id",
                "challenge",
                "audience",
                "issued_at",
                "expires_at",
            }
            or document.get("profile") != profile
            or document.get("purpose") != purpose
            or document.get("installation_id") != installation_id
            or document.get("audience") != audience
            or not isinstance(challenge, str)
            or not isinstance(document.get("issued_at"), str)
            or not isinstance(document.get("expires_at"), str)
        ):
            raise HostedGrantUnavailable("Hosted refresh challenge is invalid")
        _decode_32(challenge)
        return challenge

    @staticmethod
    def _refresh_request(current: VerifiedGrantReference) -> dict[str, object]:
        if current.organization_id is None:
            raise AuthenticationStateError("Verified grant refresh context is unavailable")
        request: dict[str, object] = {
            "profile": REFRESH_PROFILE,
            "grant_type": current.grant_type,
            "current_jti": current.grant_identifier,
            "organization_id": current.organization_id,
            "installation_id": current.installation_id,
        }
        if current.grant_type == "creator_account_binding":
            if (
                current.creator_account_id is None
                or current.approval_id is None
                or current.approval_revision is None
            ):
                raise AuthenticationStateError(
                    "Verified grant refresh context is unavailable"
                )
            request.update(
                creator_account_id=current.creator_account_id,
                approval_id=current.approval_id,
                approval_revision=current.approval_revision,
            )
        elif current.grant_type == "membership_snapshot":
            if current.membership_id is None:
                raise AuthenticationStateError(
                    "Verified grant refresh context is unavailable"
                )
            request.update(
                membership_id=current.membership_id,
                ciam_issuer=current.issuer,
                ciam_subject=current.subject,
            )
        elif current.grant_type == "license_entitlement":
            if current.entitlement_id is None or current.product_id is None:
                raise AuthenticationStateError(
                    "Verified grant refresh context is unavailable"
                )
            request.update(
                entitlement_id=current.entitlement_id,
                product_id=current.product_id,
            )
        return request

    @staticmethod
    def _validated_refresh_response(
        document: Mapping[str, object], current: VerifiedGrantReference
    ) -> str:
        grant = document.get("grant")
        if (
            set(document)
            != {
                "profile",
                "request_id",
                "grant_type",
                "previous_jti",
                "grant",
                "server_time",
                "refresh_after",
            }
            or document.get("profile") != REFRESH_PROFILE
            or document.get("grant_type") != current.grant_type
            or document.get("previous_jti") != current.grant_identifier
            or not isinstance(document.get("request_id"), str)
            or not isinstance(grant, str)
            or not isinstance(document.get("server_time"), str)
            or not isinstance(document.get("refresh_after"), str)
        ):
            raise HostedGrantUnavailable("Hosted refresh response is invalid")
        return grant

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Grant verification clock must be timezone-aware")
        return value.astimezone(timezone.utc)


def _media_type(content_type: str) -> str:
    """Return the lowercased media type without parameters."""
    return content_type.split(";", 1)[0].strip().lower()


def _retryable(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500


def _contract_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Onboarding progress timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_contract_timestamp(value: str) -> datetime:
    if _TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("Contract timestamp is invalid")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _response_object(response: TransportResponse) -> dict[str, object]:
    try:
        value = json.loads(
            response.body.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeError, ValueError, RecursionError):
        raise HostedGrantUnavailable("Hosted response is invalid") from None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise HostedGrantUnavailable("Hosted response is invalid")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("Duplicate response member")
        value[key] = member
    return value


def _public_jwk(reference: InstallationKeyReference) -> dict[str, str]:
    try:
        value = json.loads(reference.public_key_jwk)
    except json.JSONDecodeError:
        raise HostedGrantUnavailable("Installation public key is invalid") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"crv", "kid", "kty", "x", "y"}
        or value.get("kid") != reference.installation_key_id
        or not all(isinstance(item, str) for item in value.values())
    ):
        raise HostedGrantUnavailable("Installation public key is invalid")
    return {name: value[name] for name in ("crv", "kty", "x", "y")}


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_32(value: str) -> bytes:
    if not isinstance(value, str) or len(value) != 43 or "=" in value:
        raise ValueError("Base64url value is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, binascii.Error):
        raise ValueError("Base64url value is invalid") from None
    if len(decoded) != 32 or _b64url(decoded) != value:
        raise ValueError("Base64url value is invalid")
    return decoded


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _untrusted_payload(token: str) -> dict[str, object]:
    try:
        segments = token.split(".")
        if len(segments) != 3:
            raise ValueError
        encoded = segments[1]
        payload_bytes = base64.urlsafe_b64decode(
            encoded + "=" * ((4 - len(encoded) % 4) % 4)
        )
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
        raise GrantVerificationRefused("invalid_compact_jws") from None
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise GrantVerificationRefused("invalid_json")
    return payload


def _required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise GrantVerificationRefused("schema_invalid")
    return value


def _required_int(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GrantVerificationRefused("schema_invalid")
    return value


def _required_string_tuple(
    payload: Mapping[str, object], name: str
) -> tuple[str, ...]:
    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GrantVerificationRefused("schema_invalid")
    return tuple(value)


def _expected_subject(
    grant_type: str,
    payload: Mapping[str, object],
    *,
    organization_id: str,
    installation_id: str,
    external_issuer: str,
    external_subject: str,
) -> str:
    if grant_type == "installation_grant":
        return f"installation:{installation_id}"
    if grant_type == "creator_account_binding":
        account_id = _required_string(payload, "creator_account_id")
        return f"installation:{installation_id}:creator:{account_id}"
    if grant_type == "license_entitlement":
        return f"organization:{organization_id}:installation:{installation_id}"
    if grant_type == "membership_snapshot":
        material = _canonical_json(
            {
                "issuer": external_issuer,
                "subject": external_subject,
            }
        )
        return f"principal:{_b64url(material)}"
    raise GrantVerificationRefused("unsupported_grant_type")
