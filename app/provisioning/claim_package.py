"""Bounded decoder for the pasted installation-claim package."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from dataclasses import dataclass
from typing import Literal

from app.security.hosted_grants import DeviceMetadata, InstallationClaim


CLAIM_PACKAGE_PROFILE = "urn:bridge-clean:installation-claim-package:v1"

# The strict document with maximum-length bindings is 771 bytes, or 1028
# canonical base64url characters.
MAX_PACKAGE_CHARACTERS = 1400

_CLAIM_FIELDS = (
    "claim_id",
    "claim_secret",
    "challenge",
    "onboarding_transaction_id",
    "organization_id",
    "installation_id",
    "consume_path",
)
_PACKAGE_KEYS = frozenset(_CLAIM_FIELDS) | {"profile"}
_DURABLE_COORDINATES = (
    "claim_id",
    "onboarding_transaction_id",
    "organization_id",
    "installation_id",
)
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LOCAL_PLATFORMS = {"win32": "windows", "darwin": "macos", "linux": "linux"}

ClaimPackageRefusal = Literal[
    "size",
    "encoding",
    "profile",
    "schema",
    "device",
    "consumed",
]


class ClaimPackageError(ValueError):
    """Refusal carrying a fixed message and a nonsecret reason code."""

    def __init__(self, reason: ClaimPackageRefusal) -> None:
        super().__init__("Installation claim package is invalid")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ClaimRecoveryCoordinates:
    """Nonsecret claim coordinates a durable provisioning store may hold."""

    claim_id: str
    onboarding_transaction_id: str
    organization_id: str
    installation_id: str


class DecodedClaimPackage:
    """One decoded claim: nonsecret coordinates plus a single-use in-memory claim."""

    __slots__ = ("_claim", "_coordinates")

    def __init__(self, claim: InstallationClaim) -> None:
        self._claim: InstallationClaim | None = claim
        self._coordinates = ClaimRecoveryCoordinates(
            claim_id=claim.claim_id,
            onboarding_transaction_id=claim.onboarding_transaction_id,
            organization_id=claim.organization_id,
            installation_id=claim.installation_id,
        )

    @property
    def coordinates(self) -> ClaimRecoveryCoordinates:
        return self._coordinates

    @property
    def released(self) -> bool:
        return self._claim is None

    def durable_state(self) -> dict[str, str]:
        """Serialize exactly the nonsecret recovery coordinates."""
        return {
            name: getattr(self._coordinates, name) for name in _DURABLE_COORDINATES
        }

    def release_claim(self) -> InstallationClaim:
        """Hand the claim to consumption once and drop the local reference."""
        claim = self._claim
        if claim is None:
            raise ClaimPackageError("consumed")
        self._claim = None
        return claim

    def clear(self) -> None:
        """Drop the in-memory claim after refusal or consumption."""
        self._claim = None

    def __repr__(self) -> str:
        return (
            f"<DecodedClaimPackage claim_id={self._coordinates.claim_id!r} "
            f"released={self.released}>"
        )

    def __reduce__(self) -> object:
        raise TypeError("Decoded claim packages are not serializable")


def decode_claim_package(package: str) -> DecodedClaimPackage:
    """Decode one pasted package into the existing installation-claim value object."""
    if not isinstance(package, str) or not 1 <= len(package) <= MAX_PACKAGE_CHARACTERS:
        raise ClaimPackageError("size")
    document = _decode_document(package)
    if document.get("profile") != CLAIM_PACKAGE_PROFILE:
        raise ClaimPackageError("profile")
    if set(document) != _PACKAGE_KEYS:
        raise ClaimPackageError("schema")
    values: dict[str, str] = {}
    for name in _CLAIM_FIELDS:
        value = document.get(name)
        if not isinstance(value, str):
            raise ClaimPackageError("schema")
        values[name] = value
    try:
        claim = InstallationClaim(**values)
    except (TypeError, ValueError):
        raise ClaimPackageError("schema") from None
    return DecodedClaimPackage(claim)


def local_device_metadata(
    *,
    product_version: str,
    display_name: str,
    platform: str | None = None,
) -> DeviceMetadata:
    """Build device metadata from local values; the package supplies none of them."""
    resolved = platform if platform is not None else _LOCAL_PLATFORMS.get(sys.platform)
    if resolved is None:
        raise ClaimPackageError("device")
    try:
        return DeviceMetadata(resolved, product_version, display_name)  # type: ignore[arg-type]
    except ValueError:
        raise ClaimPackageError("device") from None


def _decode_document(package: str) -> dict[str, object]:
    if _BASE64URL_RE.fullmatch(package) is None or len(package) % 4 == 1:
        raise ClaimPackageError("encoding")
    try:
        decoded = base64.urlsafe_b64decode(package + "=" * (-len(package) % 4))
    except (ValueError, binascii.Error):
        raise ClaimPackageError("encoding") from None
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != package:
        raise ClaimPackageError("encoding")
    try:
        document = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ClaimPackageError("encoding") from None
    if not isinstance(document, dict) or not all(
        isinstance(key, str) for key in document
    ):
        raise ClaimPackageError("schema")
    return document
