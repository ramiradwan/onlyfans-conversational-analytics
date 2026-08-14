"""Bounded, canonical decoding of the pasted installation-claim package."""

from __future__ import annotations

import base64
import json
import pickle
from dataclasses import asdict

import pytest

from app.provisioning.claim_package import (
    CLAIM_PACKAGE_PROFILE,
    MAX_PACKAGE_CHARACTERS,
    ClaimPackageError,
    decode_claim_package,
    local_device_metadata,
)


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_CLAIM_ID = "0198a1b2-c3d4-7300-8000-000000000001"
_TRANSACTION_ID = "0198a1b2-c3d4-7400-8000-000000000001"
_CANARY_SECRET = (
    base64.urlsafe_b64encode(b"claim-secret-canary-never-stored")
    .rstrip(b"=")
    .decode("ascii")
)
_CHALLENGE = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "profile": CLAIM_PACKAGE_PROFILE,
        "claim_id": _CLAIM_ID,
        "claim_secret": _CANARY_SECRET,
        "challenge": _CHALLENGE,
        "onboarding_transaction_id": _TRANSACTION_ID,
        "organization_id": "org-1",
        "installation_id": "install-1",
        "consume_path": f"/v1/installation-claims/{_CLAIM_ID}:consume",
    }
    document.update(overrides)
    return document


def _encode(document: object, *, pad: int = 0) -> str:
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8") + b" " * pad
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _encoded_with_spare_bits(document: object) -> str:
    """Encode so the final character carries unused bits."""
    for pad in range(3):
        text = _encode(document, pad=pad)
        if len(text) % 4 in {2, 3}:
            return text
    raise AssertionError("no spare-bit encoding available")


def _spare_bit_variant(text: str) -> str:
    """Change only the unused trailing bits of the final character."""
    group = 16 if len(text) % 4 == 2 else 4
    index = _ALPHABET.index(text[-1])
    replacement = (index // group) * group + ((index % group) + 1) % group
    return text[:-1] + _ALPHABET[replacement]


def test_valid_package_decodes_into_the_existing_claim_value_object() -> None:
    package = decode_claim_package(_encode(_document()))

    assert package.coordinates.claim_id == _CLAIM_ID
    assert package.coordinates.onboarding_transaction_id == _TRANSACTION_ID
    assert package.coordinates.organization_id == "org-1"
    assert package.coordinates.installation_id == "install-1"

    claim = package.release_claim()
    assert claim.claim_id == _CLAIM_ID
    assert claim.claim_secret == _CANARY_SECRET
    assert claim.challenge == _CHALLENGE
    assert claim.consume_path == f"/v1/installation-claims/{_CLAIM_ID}:consume"


def test_unknown_profile_version_is_refused() -> None:
    package = _encode(_document(profile="urn:bridge-clean:installation-claim-package:v2"))

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(package)

    assert refusal.value.reason == "profile"


def test_missing_profile_is_refused() -> None:
    document = _document()
    del document["profile"]

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(_encode(document))

    assert refusal.value.reason == "profile"


def test_unknown_key_is_refused() -> None:
    package = _encode(_document(device={"platform": "windows"}))

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(package)

    assert refusal.value.reason == "schema"


def test_missing_required_key_is_refused() -> None:
    document = _document()
    del document["challenge"]

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(_encode(document))

    assert refusal.value.reason == "schema"


def test_non_canonical_trailing_bits_are_refused() -> None:
    canonical = _encoded_with_spare_bits(_document())
    variant = _spare_bit_variant(canonical)
    padding = "=" * (-len(variant) % 4)
    assert base64.urlsafe_b64decode(variant + padding) == base64.urlsafe_b64decode(
        canonical + padding
    )

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(variant)

    assert refusal.value.reason == "encoding"
    assert decode_claim_package(canonical).coordinates.claim_id == _CLAIM_ID


@pytest.mark.parametrize("suffix", ["=", "==", "\n", " ", "+", "/"])
def test_package_outside_the_canonical_alphabet_is_refused(suffix: str) -> None:
    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(_encode(_document()) + suffix)

    assert refusal.value.reason == "encoding"


def test_oversize_package_is_refused_before_decoding() -> None:
    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package("A" * (MAX_PACKAGE_CHARACTERS + 1))

    assert refusal.value.reason == "size"


def test_empty_package_is_refused() -> None:
    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package("")

    assert refusal.value.reason == "size"


def test_maximum_length_bindings_fit_the_size_bound() -> None:
    package = _encode(
        _document(
            onboarding_transaction_id="t" * 128,
            organization_id="o" * 128,
            installation_id="i" * 128,
        )
    )
    assert len(package) <= MAX_PACKAGE_CHARACTERS

    assert decode_claim_package(package).coordinates.organization_id == "o" * 128


def test_claim_secret_never_reaches_durable_state_or_representations() -> None:
    package = decode_claim_package(_encode(_document()))

    state = package.durable_state()
    assert set(state) == {
        "claim_id",
        "onboarding_transaction_id",
        "organization_id",
        "installation_id",
    }
    assert _CANARY_SECRET not in json.dumps(state)
    assert _CANARY_SECRET not in repr(package)
    assert _CANARY_SECRET not in str(package)
    assert _CANARY_SECRET not in repr(package.coordinates)
    assert _CANARY_SECRET not in json.dumps(asdict(package.coordinates))

    with pytest.raises(TypeError):
        pickle.dumps(package)

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(_encode(_document(organization_id="!invalid")))
    assert _CANARY_SECRET not in str(refusal.value)
    assert _CANARY_SECRET not in repr(refusal.value)


def test_claim_is_released_exactly_once() -> None:
    package = decode_claim_package(_encode(_document()))

    assert package.release_claim().claim_secret == _CANARY_SECRET
    assert package.released is True

    with pytest.raises(ClaimPackageError) as refusal:
        package.release_claim()

    assert refusal.value.reason == "consumed"


def test_clear_drops_the_in_memory_claim() -> None:
    package = decode_claim_package(_encode(_document()))

    package.clear()

    assert package.released is True
    assert package.durable_state()["claim_id"] == _CLAIM_ID
    with pytest.raises(ClaimPackageError):
        package.release_claim()


@pytest.mark.parametrize(
    "secret",
    [
        _CANARY_SECRET[:-1],
        _CANARY_SECRET + "A",
        base64.urlsafe_b64encode(b"claim-secret-canary-never-stored").decode("ascii"),
        base64.urlsafe_b64encode(b"short").rstrip(b"=").decode("ascii"),
    ],
)
def test_non_canonical_claim_secret_is_refused(secret: str) -> None:
    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(_encode(_document(claim_secret=secret)))

    assert refusal.value.reason == "schema"


def test_consume_path_mismatch_is_refused() -> None:
    package = _encode(_document(consume_path="/v1/installation-claims/other:consume"))

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(package)

    assert refusal.value.reason == "schema"


def test_non_string_field_is_refused() -> None:
    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(_encode(_document(installation_id=17)))

    assert refusal.value.reason == "schema"


@pytest.mark.parametrize("document", [["claim"], "claim", 17])
def test_non_object_document_is_refused(document: object) -> None:
    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(_encode(document))

    assert refusal.value.reason == "schema"


def test_non_json_payload_is_refused() -> None:
    text = base64.urlsafe_b64encode(b"\xff\xfe not json").rstrip(b"=").decode("ascii")

    with pytest.raises(ClaimPackageError) as refusal:
        decode_claim_package(text)

    assert refusal.value.reason == "encoding"


def test_local_device_metadata_uses_only_local_values() -> None:
    device = local_device_metadata(
        product_version="1.0.0",
        display_name="Local workstation",
        platform="windows",
    )

    assert (device.platform, device.product_version, device.display_name) == (
        "windows",
        "1.0.0",
        "Local workstation",
    )


@pytest.mark.parametrize(
    "platform,product_version,display_name",
    [
        ("solaris", "1.0.0", "Local workstation"),
        ("windows", "not a version!", "Local workstation"),
        ("windows", "1.0.0", ""),
    ],
)
def test_invalid_device_metadata_is_refused(
    platform: str, product_version: str, display_name: str
) -> None:
    with pytest.raises(ClaimPackageError) as refusal:
        local_device_metadata(
            product_version=product_version,
            display_name=display_name,
            platform=platform,
        )

    assert refusal.value.reason == "device"
