from dataclasses import dataclass, field
import secrets

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.security.companion_noise import (
    CompanionNoiseError,
    create_noise_responder,
    qualification_report,
)
from app.security.companion_pairing import NOISE_KEY_PURPOSE
from app.security.local_data_key import protect_local_secret


@dataclass(frozen=True)
class Material:
    wrapped_brain_noise_private_key: bytes = field(repr=False)
    agent_noise_public_key: bytes
    pairing_digest: bytes


def material(purpose=NOISE_KEY_PURPOSE):
    return Material(
        protect_local_secret(
            X25519PrivateKey.generate().private_bytes_raw(), purpose=purpose
        ),
        X25519PrivateKey.generate().public_key().public_bytes_raw(),
        secrets.token_bytes(32),
    )


def test_production_factory_rejects_wrong_protection_purpose_without_details():
    with pytest.raises(
        CompanionNoiseError, match="^companion_noise_unavailable$"
    ) as error:
        create_noise_responder(material("different-purpose/v1"))
    assert error.value.__suppress_context__


def test_production_factory_constructs_a_fresh_native_session_for_each_connection():
    pin = material()
    first, second = create_noise_responder(pin), create_noise_responder(pin)
    try:
        first.close()
        assert first.closed()
        assert not second.closed()
    finally:
        second.close()


def test_production_native_probe_emits_only_artifact_identity_and_verdicts():
    report = qualification_report()
    assert set(report) == {
        "schema",
        "module",
        "module_sha256",
        "suite",
        "native_module_loaded",
        "protected_factory",
        "typed_payload_free_refusal",
        "closed_after_refusal",
    }
    assert report["native_module_loaded"] is True
    assert report["typed_payload_free_refusal"] is True
    assert report["closed_after_refusal"] is True
    assert len(report["module_sha256"]) == 64
