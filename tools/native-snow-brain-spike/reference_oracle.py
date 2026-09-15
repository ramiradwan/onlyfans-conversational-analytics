from __future__ import annotations

import json

import ofca_native_snow
from noise.connection import Keypair, NoiseConnection

from frozen_server import (
    AGENT_PUBLIC,
    BRAIN_PRIVATE,
    CLIENT_READY,
    PAIRING_DIGEST,
    SERVER_READY,
    prologue,
)

SUITE = b"Noise_KK_25519_ChaChaPoly_SHA256"
AGENT_PRIVATE = bytes.fromhex("101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f")
BRAIN_PUBLIC = bytes.fromhex("79a631eede1bf9c98f12032cdeadd0e7a079398fc786b88cc846ec89af85a51a")


def initiator() -> NoiseConnection:
    conn = NoiseConnection.from_name(SUITE)
    conn.set_as_initiator()
    conn.set_keypair_from_private_bytes(Keypair.STATIC, AGENT_PRIVATE)
    conn.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, BRAIN_PUBLIC)
    conn.set_prologue(prologue(PAIRING_DIGEST))
    conn.start_handshake()
    return conn


def exchange() -> dict[str, object]:
    reference = initiator()
    brain = ofca_native_snow.NoiseResponder(BRAIN_PRIVATE, AGENT_PUBLIC, prologue())
    first = bytes(reference.write_message(b""))
    brain.consume_first_handshake(first)
    second = bytes(brain.produce_responder_handshake())
    reference.read_message(second)
    native_hash = bytes(brain.handshake_hash())
    reference_hash = None
    getter = getattr(reference, "get_handshake_hash", None)
    if callable(getter):
        reference_hash = bytes(getter())
        if reference_hash != native_hash:
            raise AssertionError("independent_handshake_hash_mismatch")
    brain.enter_transport()

    client_ready = bytes(reference.encrypt(CLIENT_READY))
    if bytes(brain.decrypt_transport(client_ready)) != CLIENT_READY:
        raise AssertionError("independent_client_confirmation_mismatch")
    server_ready = bytes(brain.encrypt_transport(SERVER_READY))
    if bytes(reference.decrypt(server_ready)) != SERVER_READY:
        raise AssertionError("independent_server_confirmation_mismatch")

    application = b"\x01independent-reference"
    encrypted = bytes(reference.encrypt(application))
    if bytes(brain.decrypt_transport(encrypted)) != application:
        raise AssertionError("independent_agent_to_brain_mismatch")
    reply = b"\x01native-snow"
    if bytes(reference.decrypt(bytes(brain.encrypt_transport(reply)))) != reply:
        raise AssertionError("independent_brain_to_agent_mismatch")
    brain.close()

    reference2 = initiator()
    brain2 = ofca_native_snow.NoiseResponder(BRAIN_PRIVATE, AGENT_PUBLIC, prologue())
    first2 = bytes(reference2.write_message(b""))
    brain2.consume_first_handshake(first2)
    second2 = bytes(brain2.produce_responder_handshake())
    reference2.read_message(second2)
    try:
        brain2.consume_first_handshake(first2)
    except ofca_native_snow.NoiseStateError as exc:
        if exc.args != () or not brain2.closed():
            raise AssertionError("completed_state_not_destroyed") from exc
    else:
        raise AssertionError("completed_handshake_reuse_accepted")

    return {
        "result": "passed",
        "oracle": "noiseprotocol==0.3.1",
        "native_handshake_hash": native_hash.hex(),
        "reference_handshake_hash_available": reference_hash is not None,
        "reference_handshake_hash": reference_hash.hex() if reference_hash is not None else None,
    }


if __name__ == "__main__":
    print(json.dumps(exchange(), sort_keys=True))
