"""Offline authentication observations for exact caller-pinned profile bytes.

Callers supply dedicated NLP keys and local resource policy. Authentication
does not establish qualification, freshness, installation or execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from app.nlp.profiles import (
    AnalysisProfile, BASELINE_RESOURCE_POLICY, ResourcePolicy, canonical_bytes,
    validate_resource_policy,
)
from app.security import signed_objects

PROFILE_JWS_TYPE = "nlp-profile.v2+jws"
MAX_PROFILE_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE_BYTES = ((MAX_PROFILE_BYTES * 4 + 2) // 3
                      + (512 * 4 + 2) // 3 + (64 * 4 + 2) // 3 + 2)
MAX_NLP_KEYS = 8


class ProfileAuthenticationRejected(ValueError):
    def __init__(self):
        super().__init__("NLP profile authentication failed")


@dataclass(frozen=True)
class AuthenticatedProfile:
    """An immutable observation, not an execution or release-admission token."""
    profile: AnalysisProfile
    profile_sha256: str
    kid: str


def _keys(value):
    signed_objects.require(type(value) in (tuple, list) and 1 <= len(value) <= MAX_NLP_KEYS)
    result = []
    seen = set()
    for key in value:
        signed_objects.require(type(key) is dict and set(key) == {"kid", "jwk"})
        kid = key["kid"]
        signed_objects.require(type(kid) is str and signed_objects.IDENTIFIER.fullmatch(kid) is not None
                               and kid not in seen)
        signed_objects.public_key(key["jwk"])
        seen.add(kid)
        result.append({"kid": kid, "jwk": dict(key["jwk"])})
    return result


def _header_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _profile(raw):
    signed_objects.parse(raw, maximum=MAX_PROFILE_BYTES)
    profile = AnalysisProfile.model_validate_json(raw)
    signed_objects.require(canonical_bytes(profile) == raw)
    return profile


def authenticate_profile(compact_jws: bytes, *, trusted_nlp_keys,
                         expected_profile_sha256: str,
                         resource_policy: ResourcePolicy = BASELINE_RESOURCE_POLICY) -> AuthenticatedProfile:
    """Verify one complete descriptor under explicit caller-owned inputs.

    Exact replay may authenticate again. No sequence, expiry or withdrawal
    state is inferred from a profile revision or from a valid signature.
    """
    try:
        signed_objects.require(type(compact_jws) is bytes and 0 < len(compact_jws) <= MAX_ENVELOPE_BYTES)
        signed_objects.require(type(expected_profile_sha256) is str
                               and re.fullmatch(r"[0-9a-f]{64}", expected_profile_sha256) is not None)
        signed_objects.require(type(resource_policy) is ResourcePolicy)
        policy = ResourcePolicy.model_validate(resource_policy.model_dump(mode="python", warnings=False))
        keys = _keys(trusted_nlp_keys)
        verified = signed_objects.verify_compact(
            compact_jws.decode("ascii"), typ=PROFILE_JWS_TYPE, keys=keys,
            maximum=MAX_PROFILE_BYTES, envelope_limit=MAX_ENVELOPE_BYTES,
            header_canonicalizer=_header_bytes, payload_validator=_profile,
        )
        signed_objects.require(verified.payload_sha256 == expected_profile_sha256)
        validate_resource_policy(verified.payload, policy)
        return AuthenticatedProfile(verified.payload, verified.payload_sha256, verified.kid)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ProfileAuthenticationRejected() from None
