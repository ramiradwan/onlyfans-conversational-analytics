"""Load the pinned native Noise responder from protected companion pin material."""

from __future__ import annotations

from typing import Protocol

from app.security.companion_pairing import NOISE_KEY_PURPOSE
from app.security.companion_pairing_proof import session_prologue
from app.security.local_data_key import unprotect_local_secret


class _PinnedMaterial(Protocol):
    wrapped_brain_noise_private_key: bytes
    agent_noise_public_key: bytes
    pairing_digest: bytes


class CompanionNoiseError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("companion_noise_unavailable")


def create_noise_responder(pin: _PinnedMaterial):
    """Open only the pin's purpose-protected key; native Snow owns the session."""
    private = bytearray()
    try:
        from ofca_native_snow import NoiseResponder

        private.extend(
            unprotect_local_secret(
                pin.wrapped_brain_noise_private_key,
                purpose=NOISE_KEY_PURPOSE,
            )
        )
        return NoiseResponder(
            bytes(private),
            pin.agent_noise_public_key,
            session_prologue(pin.pairing_digest),
        )
    except Exception:
        raise CompanionNoiseError() from None
    finally:
        private[:] = b"\x00" * len(private)


def qualification_report() -> dict[str, object]:
    """Probe the production factory with ephemeral keys and payload-free results."""
    import hashlib
    import importlib
    from dataclasses import dataclass, field
    from importlib.machinery import EXTENSION_SUFFIXES
    from pathlib import Path
    import secrets

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from app.security.local_data_key import protect_local_secret

    @dataclass(frozen=True, slots=True)
    class ProbePin:
        wrapped_brain_noise_private_key: bytes = field(repr=False)
        agent_noise_public_key: bytes
        pairing_digest: bytes

    native = importlib.import_module("ofca_native_snow.ofca_native_snow")
    module_file = Path(native.__file__)
    if not any(str(module_file).endswith(suffix) for suffix in EXTENSION_SUFFIXES):
        raise CompanionNoiseError()
    private = bytearray(X25519PrivateKey.generate().private_bytes_raw())
    try:
        wrapped = protect_local_secret(bytes(private), purpose=NOISE_KEY_PURPOSE)
    finally:
        private[:] = b"\x00" * len(private)
    pin = ProbePin(
        wrapped,
        X25519PrivateKey.generate().public_key().public_bytes_raw(),
        secrets.token_bytes(32),
    )
    responder = create_noise_responder(pin)
    refused = False
    try:
        responder.consume_first_handshake(b"invalid-handshake")
    except native.NoiseAuthenticationError as error:
        refused = error.args == ()
    finally:
        responder.close()
    if not refused or not responder.closed():
        raise CompanionNoiseError()
    return {
        "schema": "ofca-companion-native-runtime/v1",
        "module": "ofca_native_snow",
        "module_sha256": hashlib.sha256(module_file.read_bytes()).hexdigest(),
        "suite": native.SUITE,
        "native_module_loaded": True,
        "protected_factory": True,
        "typed_payload_free_refusal": refused,
        "closed_after_refusal": responder.closed(),
    }
