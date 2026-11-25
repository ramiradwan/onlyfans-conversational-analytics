"""Verify purpose-bound public credential records for the local authority owner."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

import rfc8785

from app.security.account_passkey import (
    ACCESS_PROFILE,
    BINDING_PROFILE,
    RESPONSE_LIMIT,
    WITHDRAWAL_PROFILE,
    AccountPasskeySchemas,
    is_low_s,
)
from app.security.account_passkey_webauthn import binding_public_key
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.signed_objects import (
    SignedObjectRejected,
    decode,
    encode,
    parse,
    public_key,
    require,
    verify_compact,
)
from app.security.webauthn import WebAuthnVerificationError

MAX_INITIAL_ACCESS_SECONDS = 300
_ISSUER = "urn:bridge-clean:commercial-control-plane"
_PURPOSES = {
    BINDING_PROFILE: ("account-passkey-local-binding", "pb", "local-binding-payload"),
    ACCESS_PROFILE: ("account-passkey-initial-local-access", "pa", "initial-local-access-payload"),
    WITHDRAWAL_PROFILE: ("account-passkey-local-withdrawal", "pw", "local-withdrawal-payload"),
}


@dataclass(frozen=True, slots=True)
class BindingScope:
    subject: str
    installation_id: str
    installation_key_jkt: str
    credential_id: str
    rp_id: str


@dataclass(frozen=True, slots=True)
class VerifiedPasskeyRecord:
    compact: str = field(repr=False)
    digest: str
    payload: Mapping[str, Any] = field(repr=False)

    @property
    def generation(self) -> int:
        return self.payload["generation"]


def record_digest(compact: str) -> str:
    """Hash the exact compact serialization without decoding or re-encoding it."""
    if not isinstance(compact, str) or not compact.isascii() or len(compact) > RESPONSE_LIMIT:
        raise SignedObjectRejected()
    return hashlib.sha256(compact.encode("ascii")).hexdigest()


class AccountPasskeyRecordVerifier:
    """Use the trust set selected by the installed release, never a delivered key."""

    def __init__(
        self,
        schemas: AccountPasskeySchemas,
        trust_bytes: bytes,
        *,
        pinned_sha256: str,
        environment: str,
        minimum_sequence: int,
    ) -> None:
        require(hmac.compare_digest(hashlib.sha256(trust_bytes).hexdigest(), pinned_sha256))
        try:
            trust = schemas.validate("local-trust-set", parse(trust_bytes, maximum=131_072))
        except HostedGrantUnavailable:
            raise SignedObjectRejected() from None
        require(
            trust["environment"] == environment
            and trust["production_usable"] is (environment == "production")
            and type(trust["sequence"]) is int
            and trust["sequence"] >= minimum_sequence
        )
        keys = {}
        thumbprints = set()
        purpose_codes = {purpose: code for purpose, code, _ in _PURPOSES.values()}
        for key in trust["keys"]:
            jwk = key["public_jwk"]
            public_key(jwk)
            thumbprint = hashlib.sha256(rfc8785.dumps(jwk)).digest()
            retire_at = key.get("retire_at")
            require(
                key["kid"] not in keys
                and thumbprint not in thumbprints
                and decode(key["thumbprint"], 32) == thumbprint
                and key["kid"]
                == "bc1." + purpose_codes[key["purpose"]] + "." + encode(thumbprint[:16])
                and type(key["not_before"]) is int
                and (retire_at is None or type(retire_at) is int and retire_at > key["not_before"])
            )
            keys[key["kid"]] = key
            thumbprints.add(thumbprint)
        self._schemas = schemas
        self._keys = keys
        self.sequence = trust["sequence"]

    def verify(
        self,
        compact: str,
        *,
        profile: str,
        scope: BindingScope,
        now: int,
    ) -> VerifiedPasskeyRecord:
        digest = record_digest(compact)
        purpose, _, schema = _PURPOSES[profile]
        segments = compact.split(".")
        require(len(segments) == 3 and is_low_s(decode(segments[2], 64)))
        keys = tuple(
            {"kid": key["kid"], "jwk": key["public_jwk"]}
            for key in self._keys.values()
            if key["purpose"] == purpose and key["status"] != "revoked"
        )

        def validate(raw: bytes) -> dict[str, Any]:
            try:
                return self._schemas.validate(
                    schema,
                    parse(raw, maximum=RESPONSE_LIMIT, canonicalizer=rfc8785.dumps),
                )
            except HostedGrantUnavailable:
                raise SignedObjectRejected() from None

        result = verify_compact(
            compact,
            typ=profile,
            keys=keys,
            maximum=RESPONSE_LIMIT,
            envelope_limit=RESPONSE_LIMIT,
            header_canonicalizer=rfc8785.dumps,
            payload_validator=validate,
        )
        p = result.payload
        key = self._keys[result.kid]
        require(
            p["profile"] == profile
            and p["iss"] == _ISSUER
            and p["aud"] == "urn:bridge-clean:local-brain:" + purpose
            and p["purpose"] == purpose
        )
        require(
            (
                p["sub"],
                p["installation_id"],
                p["installation_key_jkt"],
                p["credential_id"],
                p["rp_id"],
            )
            == (
                scope.subject,
                scope.installation_id,
                scope.installation_key_jkt,
                scope.credential_id,
                scope.rp_id,
            )
        )
        require(len(decode(p["sub"], 32)) == len(decode(p["installation_key_jkt"], 32)) == 32)
        decode(p["credential_id"], 1023)
        iat = p["iat"]
        require(
            type(p["generation"]) is int and type(iat) is int and key["not_before"] <= iat <= now
        )
        if "retire_at" in key:
            require(iat < key["retire_at"])
        if profile == BINDING_PROFILE:
            try:
                binding_public_key(p["credential_public_key_cose"])
            except WebAuthnVerificationError:
                raise SignedObjectRejected() from None
        if profile == ACCESS_PROFILE:
            require(
                all(type(p[name]) is int for name in ("exp", "verified_at", "source_expires_at"))
                and len(decode(p["attempt_id"], 32))
                == len(decode(p["initial_access_nonce"], 32))
                == 32
                and p["verified_at"] <= iat <= now < p["exp"]
                and p["exp"] <= min(p["source_expires_at"], p["verified_at"] + MAX_INITIAL_ACCESS_SECONDS)
            )
        return VerifiedPasskeyRecord(compact, digest, MappingProxyType(p))


def require_initial_binding(
    access: VerifiedPasskeyRecord,
    binding: VerifiedPasskeyRecord,
    *,
    enrollment_id: str,
    attempt_id: str,
    nonce: str,
) -> None:
    record, bound = access.payload, binding.payload
    require(
        record["profile"] == ACCESS_PROFILE
        and bound["profile"] == BINDING_PROFILE
        and record["binding_sha256"] == binding.digest
        and record["enrollment_id"] == enrollment_id
        and record["attempt_id"] == attempt_id
        and record["initial_access_nonce"] == nonce
        and all(
            record[name] == bound[name]
            for name in (
                "sub",
                "installation_id",
                "installation_key_jkt",
                "credential_id",
                "rp_id",
                "generation",
            )
        )
    )


def require_withdrawal_binding(
    withdrawal: VerifiedPasskeyRecord,
    binding: VerifiedPasskeyRecord,
) -> None:
    withdrawn, bound = withdrawal.payload, binding.payload
    require(
        withdrawn["profile"] == WITHDRAWAL_PROFILE
        and bound["profile"] == BINDING_PROFILE
        and withdrawn["withdrawn_binding_sha256"] == binding.digest
        and withdrawal.generation > binding.generation
        and all(
            withdrawn[name] == bound[name]
            for name in (
                "sub",
                "installation_id",
                "installation_key_jkt",
                "credential_id",
                "rp_id",
            )
        )
    )
