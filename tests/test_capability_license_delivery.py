from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest

from app.persistence.auth import InstallationKeyReference, VerifiedCapabilityLicenseReference
from app.security.capability_license_delivery import (
    ActivationPackage,
    CapabilityLicenseDeliveryClient,
    CapabilityLicenseDeliveryRefused,
    CapabilityLicenseDeliveryUnavailable,
    ReissuePackage,
    decode_activation_package,
    decode_reissue_package,
)
from app.security.capability_license_verifier import CapabilityLicenseVerificationContext
from app.security.hosted_grants import TransportResponse
from app.security.installation_key import InstallationProof

VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "capability-license-v1"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _encode(document: Mapping[str, object]) -> str:
    raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
    return _b64(raw)


def _context() -> CapabilityLicenseVerificationContext:
    data = json.loads((VECTORS / "accepted" / "verification-context.json").read_text("utf-8"))
    subject = data["expected_subject"]
    return CapabilityLicenseVerificationContext(
        expected_subject=subject,
        expected_organization_id=data["expected_organization_id"],
        expected_installation_id=data["expected_installation_id"],
        expected_installation_key_id=data["expected_installation_key_id"],
        expected_installation_key_jkt=data["expected_installation_key_jkt"],
        expected_seat_id=subject.split(":seat:", 1)[1].split(":capability:", 1)[0],
        expected_seat_scope=data["expected_seat_scope"],
        expected_capability=data["expected_capability"],
        expected_target_major=data["expected_target_major"],
        expected_artifact_family=data["expected_artifact_family"],
        requested_update_mode=data["requested_update_mode"],
        expected_fallback_major=data.get("expected_fallback_major"),
    )


def _key(context: CapabilityLicenseVerificationContext) -> InstallationKeyReference:
    return InstallationKeyReference(
        provider_name="test",
        provider_key_name="key",
        algorithm="ES256",
        installation_key_id=context.expected_installation_key_id,
        installation_key_jkt=context.expected_installation_key_jkt,
        public_key_jwk="{}",
        created_at=NOW,
        activated_at=NOW,
    )


class ProofAuthority:
    def __init__(self, key: InstallationKeyReference) -> None:
        self.key = key
        self.signed: list[bytes] = []

    def ensure_ready(self) -> InstallationKeyReference:
        return self.key

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        self.signed.append(challenge)
        return InstallationProof(self.key.installation_key_id, "ES256", b"\x00" * 31 + b"\x01" + b"\x00" * 31 + b"\x01")


class QueueTransport:
    def __init__(self, responses: list[TransportResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, object]]] = []

    def request(self, method: str, path: str, *, json_body: Mapping[str, object]) -> TransportResponse:
        assert method == "POST"
        self.requests.append((path, json_body))
        if not self.responses:
            raise OSError("offline")
        return self.responses.pop(0)


class Authority:
    def __init__(self, reference: VerifiedCapabilityLicenseReference) -> None:
        self.reference = reference
        self.persisted: list[VerifiedCapabilityLicenseReference] = []

    def verify(self, token: str, *, context: CapabilityLicenseVerificationContext) -> VerifiedCapabilityLicenseReference:
        assert token
        assert context.expected_installation_id == self.reference.installation_id
        return self.reference

    def persist(self, reference: VerifiedCapabilityLicenseReference) -> VerifiedCapabilityLicenseReference:
        self.persisted.append(reference)
        return reference


def _reference(context: CapabilityLicenseVerificationContext) -> VerifiedCapabilityLicenseReference:
    token = (VECTORS / "accepted" / "token.jws").read_text("ascii").strip()
    payload = json.loads((VECTORS / "accepted" / "payload.json").read_text("utf-8"))
    return VerifiedCapabilityLicenseReference(
        reference_id="caplic." + hashlib.sha256(token.encode()).hexdigest(),
        license_id=payload["license_id"],
        issuance_id=payload["issuance_id"],
        object_digest=hashlib.sha256(token.encode()).hexdigest(),
        compact_jws=token,
        subject=payload["sub"],
        organization_id=context.expected_organization_id,
        installation_id=context.expected_installation_id,
        installation_key_id=context.expected_installation_key_id,
        installation_key_jkt=context.expected_installation_key_jkt,
        seat_id=context.expected_seat_id,
        seat_scope=context.expected_seat_scope,
        capability=context.expected_capability,
        licensed_major_version=context.expected_target_major,
        compatible_artifact_family=context.expected_artifact_family,
        update_rights=True,
        fallback_major_versions=(2,),
        signer_kid="bc1.cl.zqzGpwkQlnfOpQwCLF41xA",
        verified_at=NOW,
        verification_source="conformance",
    )


def _json_response(status: int, value: Mapping[str, object]) -> TransportResponse:
    return TransportResponse(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")


def test_activation_package_is_exact_and_path_bound() -> None:
    context = _context()
    exchange = "0198a1b2-c3d4-7300-8000-000000000002"
    doc = {
        "profile": "urn:bridge-clean:capability-license-activation-package:v1",
        "exchange_id": exchange,
        "organization_id": context.expected_organization_id,
        "installation_id": context.expected_installation_id,
        "capability": "analysis-run",
        "authority_target": "capability-license",
        "proof_profile": "urn:bridge-clean:capability-license-activation-proof:v1",
        "proof_purpose": "capability-license-activate",
        "proof_challenge_path": f"/v1/installations/{context.expected_installation_id}/capability-license-proof-challenges",
        "activation_path": f"/v1/capability-license-exchanges/{exchange}:activate",
    }
    package = decode_activation_package(_encode(doc))
    assert isinstance(package, ActivationPackage)
    bad = dict(doc, activation_path="/v1/capability-license-exchanges/other:activate")
    with pytest.raises(CapabilityLicenseDeliveryRefused):
        decode_activation_package(_encode(bad))


def test_activation_proof_uses_capability_license_domain_and_persists_only_after_bindings() -> None:
    context = _context()
    key = _key(context)
    proof = ProofAuthority(key)
    reference = _reference(context)
    authority = Authority(reference)
    exchange = "0198a1b2-c3d4-7300-8000-000000000002"
    package = {
        "profile": "urn:bridge-clean:capability-license-activation-package:v1",
        "exchange_id": exchange,
        "organization_id": context.expected_organization_id,
        "installation_id": context.expected_installation_id,
        "capability": "analysis-run",
        "authority_target": "capability-license",
        "proof_profile": "urn:bridge-clean:capability-license-activation-proof:v1",
        "proof_purpose": "capability-license-activate",
        "proof_challenge_path": f"/v1/installations/{context.expected_installation_id}/capability-license-proof-challenges",
        "activation_path": f"/v1/capability-license-exchanges/{exchange}:activate",
    }
    challenge = {
        "profile": "urn:bridge-clean:capability-license-activation-proof:v1",
        "purpose": "capability-license-activate",
        "organization_id": context.expected_organization_id,
        "authority_reference_id": exchange,
        "installation_id": context.expected_installation_id,
        "proof_challenge_id": "0198a1b2-c3d4-7300-8000-000000000009",
        "challenge": "A" * 43,
        "audience": "urn:bridge-clean:commercial-control-plane:capability-license-activation",
        "issued_at": "2026-09-13T08:03:00.000Z",
        "expires_at": "2026-09-13T08:04:00.000Z",
    }
    response = {
        "profile": "urn:bridge-clean:capability-license-activation:v1",
        "exchange_id": exchange,
        "activation_id": "0198a1b2-c3d4-7300-8000-000000000003",
        "organization_id": context.expected_organization_id,
        "installation_id": context.expected_installation_id,
        "installation_key_id": context.expected_installation_key_id,
        "installation_key_jkt": context.expected_installation_key_jkt,
        "seat_id": context.expected_seat_id,
        "seat_scope": context.expected_seat_scope,
        "capability": context.expected_capability,
        "licensed_major_version": context.expected_target_major,
        "license_id": reference.license_id,
        "issuance_id": reference.issuance_id,
        "issuance_state": "current",
        "activated_at": "2026-09-13T08:04:00.000Z",
        "capability_license": reference.compact_jws,
    }
    transport = QueueTransport([_json_response(201, challenge), _json_response(201, response)])
    client = CapabilityLicenseDeliveryClient(transport, proof, authority)  # type: ignore[arg-type]

    accepted = client.activate(_encode(package), context=context)

    assert accepted == reference
    assert authority.persisted == [reference]
    assert proof.signed and proof.signed[0].startswith(b"BRIDGE-CLEAN-CAPABILITY-LICENSE-ACTIVATION-PROOF-V1\x00")
    assert not proof.signed[0].startswith(b"BRIDGE-CLEAN-INSTALLATION-PROOF-V1\x00")


def test_transport_failure_does_not_withdraw_existing_local_authority() -> None:
    context = _context()
    reference = _reference(context)
    authority = Authority(reference)
    authority.persisted.append(reference)
    exchange = "0198a1b2-c3d4-7300-8000-000000000002"
    package = {
        "profile": "urn:bridge-clean:capability-license-activation-package:v1",
        "exchange_id": exchange,
        "organization_id": context.expected_organization_id,
        "installation_id": context.expected_installation_id,
        "capability": "analysis-run",
        "authority_target": "capability-license",
        "proof_profile": "urn:bridge-clean:capability-license-activation-proof:v1",
        "proof_purpose": "capability-license-activate",
        "proof_challenge_path": f"/v1/installations/{context.expected_installation_id}/capability-license-proof-challenges",
        "activation_path": f"/v1/capability-license-exchanges/{exchange}:activate",
    }
    client = CapabilityLicenseDeliveryClient(QueueTransport([]), ProofAuthority(_key(context)), authority)  # type: ignore[arg-type]
    with pytest.raises(CapabilityLicenseDeliveryUnavailable):
        client.activate(_encode(package), context=context)
    assert authority.persisted == [reference]


def test_published_reissue_handoff_package_decodes_deterministically() -> None:
    vectors = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts" / "capability-license-hosted-api-v1" / "reissue-positive-handoff.json").read_text("utf-8")
    )
    positive = next(item for item in vectors if item.get("response"))
    encoded = positive["response"]["handoff_package_encoded"]
    package = decode_reissue_package(encoded)
    assert isinstance(package, ReissuePackage)
    assert package.replacement_installation_id == "install.replacement"
    assert package.reissue_reason == "replacement-installation"



def _replacement_context() -> CapabilityLicenseVerificationContext:
    return CapabilityLicenseVerificationContext(
        expected_subject=(
            "organization:org.acme:installation:install.replacement:"
            "seat:seat.analysis.001:capability:analysis-run"
        ),
        expected_organization_id="org.acme",
        expected_installation_id="install.replacement",
        expected_installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
        expected_installation_key_jkt="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        expected_seat_id="seat.analysis.001",
        expected_seat_scope="organization-installation-seat",
        expected_capability="analysis-run",
        expected_target_major=1,
        expected_artifact_family="analysis-artifact",
        requested_update_mode=False,
        expected_fallback_major=None,
    )


def _synthetic_reference(
    context: CapabilityLicenseVerificationContext,
    *,
    license_id: str,
    issuance_id: str,
    token: str = "signed.capability.license",
) -> VerifiedCapabilityLicenseReference:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return VerifiedCapabilityLicenseReference(
        reference_id="caplic." + digest,
        license_id=license_id,
        issuance_id=issuance_id,
        object_digest=digest,
        compact_jws=token,
        subject=context.expected_subject,
        organization_id=context.expected_organization_id,
        installation_id=context.expected_installation_id,
        installation_key_id=context.expected_installation_key_id,
        installation_key_jkt=context.expected_installation_key_jkt,
        seat_id=context.expected_seat_id,
        seat_scope=context.expected_seat_scope,
        capability=context.expected_capability,
        licensed_major_version=context.expected_target_major,
        compatible_artifact_family=context.expected_artifact_family,
        update_rights=False,
        fallback_major_versions=(),
        signer_kid="bc1.cl.test",
        verified_at=NOW,
        verification_source="conformance",
    )


def test_published_activation_package_decodes_directly() -> None:
    vectors = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "capability-license-hosted-api-v1"
            / "provisioning-quote-exchange.json"
        ).read_text("utf-8")
    )
    exchange = next(item for item in vectors if item.get("case_id") == "exchange-positive")
    response = exchange["response"]
    package = decode_activation_package(response["activation_package_encoded"])
    assert package.exchange_id == response["exchange_id"]
    assert package.installation_id == "install.primary"
    assert package.capability == "analysis-run"
    assert package.authority_target == "capability-license"


def test_published_reissue_authorization_response_is_bound_to_prior_authority() -> None:
    vectors = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "capability-license-hosted-api-v1"
            / "reissue-positive-handoff.json"
        ).read_text("utf-8")
    )
    case = next(item for item in vectors if item.get("case_id") == "reissue-authorization-positive")
    request = case["request"]
    response = case["response"]
    prior_context = CapabilityLicenseVerificationContext(
        expected_subject=(
            "organization:org.acme:installation:install.primary:"
            "seat:seat.analysis.001:capability:analysis-run"
        ),
        expected_organization_id="org.acme",
        expected_installation_id="install.primary",
        expected_installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
        expected_installation_key_jkt="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        expected_seat_id="seat.analysis.001",
        expected_seat_scope="organization-installation-seat",
        expected_capability="analysis-run",
        expected_target_major=1,
        expected_artifact_family="analysis-artifact",
        requested_update_mode=False,
    )
    prior = _synthetic_reference(
        prior_context,
        license_id=request["prior_license_id"],
        issuance_id=request["prior_issuance_id"],
    )
    transport = QueueTransport([_json_response(201, response)])
    client = CapabilityLicenseDeliveryClient(
        transport,
        ProofAuthority(_key(prior_context)),
        Authority(prior),  # type: ignore[arg-type]
    )

    authorization = client.authorize_reissue(
        prior,
        replacement_installation_id=request["replacement_installation_id"],
        customer_step_up_ref=request["customer_step_up_ref"],
        reissue_reason=request["reissue_reason"],
    )

    assert authorization.package_encoded == response["handoff_package_encoded"]
    assert authorization.package.reissue_authorization_id == response["reissue_authorization_id"]
    assert authorization.decision_audit_ref == response["decision_audit_ref"]
    path, submitted = transport.requests[0]
    assert path == f"/v1/capability-licenses/{prior.license_id}/reissue-authorizations"
    assert submitted == request


def test_published_reissue_finalization_proves_replacement_key_and_persists_replacement_only() -> None:
    handoff_vectors = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "capability-license-hosted-api-v1"
            / "reissue-positive-handoff.json"
        ).read_text("utf-8")
    )
    handoff = next(
        item for item in handoff_vectors if item.get("case_id") == "reissue-authorization-positive"
    )["response"]["handoff_package_encoded"]
    finalization_vectors = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "capability-license-hosted-api-v1"
            / "reissue-positive-finalization.json"
        ).read_text("utf-8")
    )
    challenge = next(
        item for item in finalization_vectors if item.get("case_id") == "reissue-proof-challenge-positive"
    )["response"]
    final_case = next(
        item for item in finalization_vectors if item.get("case_id") == "reissue-finalization-positive"
    )
    response = final_case["response"]
    replacement = response["replacement_issuance"]
    context = _replacement_context()
    reference = _synthetic_reference(
        context,
        license_id=replacement["license_id"],
        issuance_id=replacement["issuance_id"],
        token=response["capability_license"],
    )
    authority = Authority(reference)
    proof = ProofAuthority(_key(context))
    transport = QueueTransport([_json_response(201, challenge), _json_response(201, response)])
    client = CapabilityLicenseDeliveryClient(transport, proof, authority)  # type: ignore[arg-type]

    accepted = client.finalize_reissue(handoff, context=context)

    assert accepted == reference
    assert authority.persisted == [reference]
    assert proof.signed and proof.signed[0].startswith(
        b"BRIDGE-CLEAN-CAPABILITY-LICENSE-ACTIVATION-PROOF-V1\x00"
    )
    assert transport.requests[0][0] == (
        "/v1/installations/install.replacement/capability-license-proof-challenges"
    )
    assert transport.requests[1][0].endswith(
        "/0198a1b2-c3d4-7300-8000-000000000011:finalize"
    )
    assert transport.requests[1][1]["request"] == final_case["request"]["request"]


def test_unsigned_reissue_refusal_never_mutates_existing_signed_authority() -> None:
    context = _replacement_context()
    existing = _synthetic_reference(
        context,
        license_id="0198a1b2-c3d4-7300-8000-000000000007",
        issuance_id="0198a1b2-c3d4-7300-8000-000000000008",
    )
    authority = Authority(existing)
    authority.persisted.append(existing)
    handoff_vectors = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "capability-license-hosted-api-v1"
            / "reissue-positive-handoff.json"
        ).read_text("utf-8")
    )
    encoded = next(
        item for item in handoff_vectors if item.get("case_id") == "reissue-authorization-positive"
    )["response"]["handoff_package_encoded"]
    refusal = {"error": "authorization_superseded"}
    client = CapabilityLicenseDeliveryClient(
        QueueTransport([_json_response(409, refusal)]),
        ProofAuthority(_key(context)),
        authority,  # type: ignore[arg-type]
    )

    with pytest.raises(CapabilityLicenseDeliveryRefused):
        client.finalize_reissue(encoded, context=context)

    assert authority.persisted == [existing]
