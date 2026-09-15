"""Production pairing proof interoperability and misuse controls."""

from __future__ import annotations

import base64
import hashlib
import json
import traceback
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
)

from app.persistence.auth import SQLiteAuthenticationStore, VerifiedGrantReference
from app.security import companion_pairing_proof as pairing
from app.security.grant_verifier import GrantVerificationContext, verify_grant
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    PLATFORM_CRYPTO_PROVIDER,
    InstallationKeyAuthority,
    InstallationKeyUnavailable,
    ProviderKeyInfo,
)
from contracts.loader import verify_snapshot_integrity

ROOT = Path(__file__).resolve().parents[1] / "contracts"
ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture(scope="module")
def contract():
    # Fail before consuming vectors when any selected byte or consumer pin differs.
    verify_snapshot_integrity(ROOT)
    return {
        "vector": json.loads((ROOT / "companion-pairing-v1/vector.json").read_bytes()),
        "profile": json.loads(
            (ROOT / "companion-pairing-profile/profile.json").read_bytes()
        ),
        "trust": json.loads(
            (ROOT / "companion-pairing-v1/trust-set.json").read_bytes()
        ),
    }


class FixtureProvider:
    """Software test double for the existing non-exportable provider boundary."""

    provider_name = PLATFORM_CRYPTO_PROVIDER

    def __init__(self, contract):
        label = contract["vector"]["fixture_labels"]["installation_key"]
        material = hashlib.sha256(
            (
                contract["profile"]["test_fixture_derivation"]["label_prefix"] + label
            ).encode("ascii")
        ).digest()
        self.key = ec.derive_private_key(
            int.from_bytes(material, "big") % (ORDER - 1) + 1, ec.SECP256R1()
        )
        self.key_name = None
        self.refused = False
        self.signed_digests = []

    def key_info(self, provider_key_name):
        if provider_key_name != self.key_name:
            return None
        numbers = self.key.public_key().public_numbers()
        return ProviderKeyInfo(
            provider_name=self.provider_name,
            provider_key_name=provider_key_name,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            export_policy=0,
            hardware_backed=True,
            x=numbers.x.to_bytes(32, "big"),
            y=numbers.y.to_bytes(32, "big"),
        )

    def create_non_exportable_key(self, provider_key_name):
        self.key_name = provider_key_name
        return self.key_info(provider_key_name)

    def sign_digest(self, provider_key_name, digest):
        if self.refused:
            raise InstallationKeyUnavailable("provider-private-diagnostic-sentinel")
        self.signed_digests.append(digest)
        encoded = self.key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        r, s = decode_dss_signature(encoded)
        # Exercise the authority's normalization even if the signer chose low S.
        return r.to_bytes(32, "big") + max(s, ORDER - s).to_bytes(32, "big")


@pytest.fixture
def provider_and_key(tmp_path, contract):
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    provider = FixtureProvider(contract)
    authority = InstallationKeyAuthority(store, provider)
    reference = authority.ensure_ready()
    return provider, authority, reference


@pytest.fixture
def grant_references(contract):
    vector = contract["vector"]
    identity = vector["expected"]["identity"]
    records = {}
    for kind in ("installation_grant", "creator_account_binding"):
        token = vector["offer"][kind]
        claims = json.loads(decode(token.split(".")[1]))
        outcome = verify_grant(
            token,
            trust_set=contract["trust"],
            context=GrantVerificationContext(
                expected_grant_type=kind,
                expected_audience=contract["profile"]["grants"][kind]["audience"],
                expected_organization_id=identity["organization_id"],
                expected_installation_id=identity["installation_id"],
                expected_installation_key_id=identity["installation_key_id"],
                expected_installation_key_jkt=identity["installation_key_jkt"],
                expected_subject=claims["sub"],
                verifier_time=vector["now"],
            ),
        )
        assert outcome.valid
        records[kind] = VerifiedGrantReference(
            reference_id=kind,
            grant_identifier=claims["jti"],
            grant_type=kind,
            grant_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
            # Hosted acquisition stores the external operator identity here;
            # the signed JWS issuer/subject were checked above by verify_grant.
            issuer="https://fixture-identity.invalid",
            subject="fixture-operator",
            installation_id=claims["installation_id"],
            creator_account_id=claims.get("creator_account_id"),
            valid_from=datetime.fromtimestamp(claims["nbf"], timezone.utc),
            expires_at=datetime.fromtimestamp(claims["exp"], timezone.utc),
            verified_at=datetime.fromtimestamp(vector["now"], timezone.utc),
            organization_id=claims["organization_id"],
            installation_key_id=claims["installation_key_id"],
            installation_key_jkt=claims["installation_key_jkt"],
            compact_jws=token,
        )
    return records


@pytest.fixture
def transcript_arguments(contract, provider_and_key, grant_references):
    request = contract["vector"]["request"]
    offer = contract["vector"]["offer"]
    return {
        **grant_references,
        "installation_key": provider_and_key[2],
        "pairing_id": decode(offer["pairing_id"]),
        "generation": offer["generation"],
        "agent_installation_id": request["agent_installation_id"],
        "agent_identity_jwk": request["agent_identity_jwk"],
        "agent_noise_key": decode(request["agent_noise_key"]),
        "brain_noise_key": decode(offer["brain_noise_key"]),
        "agent_nonce": decode(request["agent_nonce"]),
        "brain_nonce": decode(offer["brain_nonce"]),
    }


def test_production_transcript_matches_pinned_cross_runtime_vectors(
    contract, transcript_arguments
):
    transcript = pairing.build_pairing_transcript(**transcript_arguments)
    expected = contract["vector"]["expected"]
    digest = pairing.pairing_digest(transcript)
    assert transcript.grant_digest.hex() == expected["grant_digest"]
    assert transcript.agent_identity_key_jkt == expected["agent_identity_key_jkt"]
    assert pairing.pairing_transcript(transcript).hex() == expected["transcript"]
    assert digest.hex() == expected["pairing_digest"]
    for role in ("brain", "agent"):
        assert (
            pairing.proof_message(role, digest).hex()
            == expected[f"{role}_proof_message"]
        )
    assert pairing.comparison_code(digest) == expected["comparison_code"]
    assert pairing.session_prologue(digest).hex() == expected["session_prologue"]
    assert pairing.SUITE == contract["profile"]["suite"]
    assert (
        pairing.MAX_GRANT_CHARACTERS
        == contract["profile"]["limits"]["max_grant_characters"]
    )


def test_signed_provider_proof_matches_contract_and_separates_purposes(
    contract,
    transcript_arguments,
    provider_and_key,
):
    provider, authority, reference = provider_and_key
    transcript = pairing.build_pairing_transcript(**transcript_arguments)
    digest = pairing.pairing_digest(transcript)
    proof = pairing.sign_brain_pairing_proof(authority, reference, transcript)
    jwk = contract["vector"]["offer"]["installation_jwk"]
    assert pairing.verify_pairing_proof(jwk, "brain", digest, proof)
    assert not pairing.verify_pairing_proof(jwk, "agent", digest, proof)
    assert (
        provider.signed_digests[-1]
        == hashlib.sha256(pairing.proof_message("brain", digest)).digest()
    )
    # An ordinary installation challenge signature is not a pairing proof.
    ordinary = authority.sign_challenge(digest)
    assert not pairing.verify_pairing_proof(
        jwk, "brain", digest, encode(ordinary.signature)
    )


@pytest.mark.parametrize("role", ["agent", "brain"])
def test_contract_proofs_verify_and_reject_high_s_and_noncanonical_encoding(
    contract, role
):
    vector = contract["vector"]
    digest = bytes.fromhex(vector["expected"]["pairing_digest"])
    signature = (
        vector["confirm"]["agent_proof"]
        if role == "agent"
        else vector["offer"]["brain_proof"]
    )
    jwk = (
        vector["request"]["agent_identity_jwk"]
        if role == "agent"
        else vector["offer"]["installation_jwk"]
    )
    assert pairing.verify_pairing_proof(jwk, role, digest, signature)
    raw = decode(signature)
    high = raw[:32] + (ORDER - int.from_bytes(raw[32:], "big")).to_bytes(32, "big")
    for invalid in (encode(high), signature + "=", signature[:-1], encode(bytes(64))):
        assert not pairing.verify_pairing_proof(jwk, role, digest, invalid)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    alias = signature[:-1] + alphabet[alphabet.index(signature[-1]) | 1]
    assert decode(alias) == raw
    assert not pairing.verify_pairing_proof(jwk, role, digest, alias)
    assert not pairing.verify_pairing_proof(jwk, role, bytes(32), signature)
    assert not pairing.verify_pairing_proof(jwk, "bridge", digest, signature)
    wrong_key = (
        vector["offer"]["installation_jwk"]
        if role == "agent"
        else vector["request"]["agent_identity_jwk"]
    )
    assert not pairing.verify_pairing_proof(wrong_key, role, digest, signature)


def test_every_transcript_field_is_bound(transcript_arguments):
    transcript = pairing.build_pairing_transcript(**transcript_arguments)
    initial_digest = pairing.pairing_digest(transcript)
    for field in fields(transcript):
        value = getattr(transcript, field.name)
        if field.name.endswith("_jkt"):
            changed = encode(bytes([decode(value)[0] ^ 1]) + decode(value)[1:])
        elif isinstance(value, str):
            changed = value + "x"
        elif isinstance(value, bytes):
            changed = bytes([value[0] ^ 1]) + value[1:]
        else:
            changed = value + 1
        assert (
            pairing.pairing_digest(replace(transcript, **{field.name: changed}))
            != initial_digest
        )


@pytest.mark.parametrize("generation", [0, -1, 2**53, True, 1.0])
def test_refuses_nonportable_generations(transcript_arguments, generation):
    with pytest.raises(
        pairing.CompanionPairingProofError, match="^pairing_generation_refused$"
    ):
        pairing.build_pairing_transcript(
            **{**transcript_arguments, "generation": generation}
        )
    transcript = pairing.build_pairing_transcript(
        **{**transcript_arguments, "generation": 2**53 - 1}
    )
    assert transcript.generation == 2**53 - 1


def test_refuses_all_small_order_encodings_and_reused_peer_material(
    contract, transcript_arguments
):
    for raw in contract["profile"]["small_order_noise_keys"]["keys"]:
        point = bytes.fromhex(raw)
        for key in (point, point[:31] + bytes([point[31] | 0x80])):
            for role in ("brain_noise_key", "agent_noise_key"):
                with pytest.raises(
                    pairing.CompanionPairingProofError, match="^pairing_key_refused$"
                ):
                    pairing.build_pairing_transcript(
                        **{**transcript_arguments, role: key}
                    )
    for name, value, code in (
        (
            "brain_noise_key",
            transcript_arguments["agent_noise_key"],
            "pairing_key_refused",
        ),
        ("brain_nonce", transcript_arguments["agent_nonce"], "pairing_nonce_refused"),
        ("pairing_id", bytes(31), "pairing_message_invalid"),
        ("agent_installation_id", "bad/id", "pairing_message_invalid"),
    ):
        with pytest.raises(pairing.CompanionPairingProofError, match=f"^{code}$"):
            pairing.build_pairing_transcript(**{**transcript_arguments, name: value})


@pytest.mark.parametrize(
    "alteration",
    ["extra", "wrong-curve", "padding", "invalid-point", "short-coordinate"],
)
def test_refuses_invalid_or_noncanonical_public_jwk(contract, alteration):
    jwk = dict(contract["vector"]["request"]["agent_identity_jwk"])
    if alteration == "extra":
        jwk["d"] = "private-key-sentinel"
    elif alteration == "wrong-curve":
        jwk["crv"] = "P-384"
    elif alteration == "padding":
        jwk["x"] += "="
    elif alteration == "invalid-point":
        jwk["x"], jwk["y"] = encode(bytes(32)), encode(bytes(32))
    else:
        jwk["x"] = encode(bytes(31))
    with pytest.raises(pairing.CompanionPairingProofError):
        pairing.jwk_thumbprint(jwk)
    assert not pairing.verify_pairing_proof(jwk, "agent", bytes(32), "bad")


@pytest.mark.parametrize(
    "name",
    [
        "organization_id",
        "installation_id",
        "installation_key_id",
        "installation_key_jkt",
        "grant_type",
        "grant_digest",
        "issuer",
        "subject",
    ],
)
def test_refuses_inconsistent_verified_reference_metadata(transcript_arguments, name):
    changed = replace(
        transcript_arguments["creator_account_binding"], **{name: "mismatch"}
    )
    with pytest.raises(
        pairing.CompanionPairingProofError, match="^pairing_grant_refused$"
    ):
        pairing.build_pairing_transcript(
            **{**transcript_arguments, "creator_account_binding": changed}
        )


@pytest.mark.parametrize(
    "condition", ["missing", "oversize", "unicode", "different-bytes", "malformed"]
)
def test_retained_token_validation_is_bounded_and_diagnostics_are_payload_free(
    transcript_arguments, condition
):
    token = {
        "missing": None,
        "oversize": "sensitive-token-sentinel" * 1000,
        "unicode": "sensitive-token-sentinel.é.x",
        "different-bytes": "different.token.bytes",
        "malformed": "sensitive-token-sentinel",
    }[condition]
    changed = replace(transcript_arguments["installation_grant"], compact_jws=token)
    with pytest.raises(pairing.CompanionPairingProofError) as caught:
        pairing.build_pairing_transcript(
            **{**transcript_arguments, "installation_grant": changed}
        )
    rendered = "".join(traceback.format_exception(caught.value))
    if token is not None and token in rendered:
        pytest.fail("retained grant appeared in diagnostic", pytrace=False)
    assert str(caught.value) == "pairing_grant_refused"


def test_identity_mismatch_refuses_before_provider_signing(
    transcript_arguments, provider_and_key
):
    provider, authority, reference = provider_and_key
    transcript = pairing.build_pairing_transcript(**transcript_arguments)
    before = len(provider.signed_digests)
    wrong = replace(transcript, installation_key_jkt=encode(bytes(32)))
    with pytest.raises(
        pairing.CompanionPairingProofError, match="^pairing_key_refused$"
    ):
        pairing.sign_brain_pairing_proof(authority, reference, wrong)
    assert len(provider.signed_digests) == before
    wrong_reference = replace(reference, installation_key_jkt=encode(bytes(32)))
    with pytest.raises(pairing.CompanionPairingProofError):
        pairing.build_pairing_transcript(
            **{**transcript_arguments, "installation_key": wrong_reference}
        )
    jwk = json.loads(reference.public_key_jwk)
    jwk["kid"] = "wrong-installation-key"
    wrong_reference = replace(reference, public_key_jwk=json.dumps(jwk))
    with pytest.raises(
        pairing.CompanionPairingProofError, match="^pairing_key_refused$"
    ):
        pairing.sign_brain_pairing_proof(authority, wrong_reference, transcript)
    assert len(provider.signed_digests) == before


@pytest.mark.parametrize("field", ["installation_key_id", "algorithm", "signature"])
def test_rejects_invalid_provider_proof_before_returning_it(
    transcript_arguments,
    provider_and_key,
    monkeypatch,
    field,
):
    _, authority, reference = provider_and_key
    transcript = pairing.build_pairing_transcript(**transcript_arguments)
    proof = authority.sign_challenge(
        pairing.proof_message("brain", pairing.pairing_digest(transcript))
    )
    invalid = replace(proof, **{field: bytes(64) if field == "signature" else "wrong"})
    monkeypatch.setattr(authority, "sign_challenge", lambda _: invalid)
    with pytest.raises(
        pairing.CompanionPairingProofError, match="^pairing_proof_refused$"
    ):
        pairing.sign_brain_pairing_proof(authority, reference, transcript)


def test_provider_refusal_suppresses_cause_and_transcript_repr_has_no_material(
    transcript_arguments,
    provider_and_key,
):
    provider, authority, reference = provider_and_key
    transcript = pairing.build_pairing_transcript(**transcript_arguments)
    provider.refused = True
    with pytest.raises(pairing.CompanionPairingProofError) as caught:
        pairing.sign_brain_pairing_proof(authority, reference, transcript)
    assert str(caught.value) == "pairing_proof_refused"
    rendered = "".join(traceback.format_exception(caught.value))
    assert "provider-private-diagnostic-sentinel" not in rendered
    assert transcript.organization_id not in repr(transcript)
    assert transcript.pairing_id.hex() not in repr(transcript)
    assert (
        str(pairing.CompanionPairingProofError("sensitive-value"))
        == "pairing_proof_refused"
    )
