from __future__ import annotations

import json
import unittest
from pathlib import Path

import ofca_native_snow

from frozen_server import (
    AGENT_PUBLIC,
    AUTH_LIMIT,
    BRAIN_PRIVATE,
    ORDINARY_LIMIT,
    prologue,
)


class NativeSnowSurfaceTests(unittest.TestCase):
    def test_suite_is_fixed_and_contract_limits_match_gate_zero(self) -> None:
        self.assertEqual(ofca_native_snow.SUITE, "Noise_KK_25519_ChaChaPoly_SHA256")
        root = Path(__file__).resolve().parents[2]
        profile = json.loads(
            (root / "contracts/companion-pairing-profile/profile.json").read_text(encoding="utf-8")
        )
        limits = profile["limits"]
        self.assertEqual(limits["max_application_record_plaintext_bytes"], ORDINARY_LIMIT)
        self.assertEqual(limits["max_authorization_record_plaintext_bytes"], AUTH_LIMIT)
        overhead = limits["noise_tag_bytes"] + limits["record_discriminator_bytes"]
        self.assertEqual(AUTH_LIMIT + overhead, limits["max_frame_bytes"])
        self.assertEqual(
            ORDINARY_LIMIT + overhead, limits["max_application_frame_bytes"]
        )
        self.assertLessEqual(limits["max_authorization_envelope_bytes"], AUTH_LIMIT)

    def test_constructor_rejects_non_contract_inputs_with_payload_free_errors(self) -> None:
        cases = [
            (BRAIN_PRIVATE[:-1], AGENT_PUBLIC, prologue()),
            (BRAIN_PRIVATE, AGENT_PUBLIC[:-1], prologue()),
            (BRAIN_PRIVATE, AGENT_PUBLIC, b"wrong"),
        ]
        for private, peer, binding in cases:
            with self.assertRaises(ofca_native_snow.NoiseInputError) as caught:
                ofca_native_snow.NoiseResponder(private, peer, binding)
            self.assertEqual(caught.exception.args, ())

    def test_use_after_close_is_typed_and_payload_free(self) -> None:
        responder = ofca_native_snow.NoiseResponder(BRAIN_PRIVATE, AGENT_PUBLIC, prologue())
        responder.close()
        with self.assertRaises(ofca_native_snow.NoiseClosedError) as caught:
            responder.consume_first_handshake(b"x")
        self.assertEqual(caught.exception.args, ())
        self.assertTrue(responder.closed())

    def test_transport_ceiling_is_one_authorization_record(self) -> None:
        # The length check precedes the transport-state check, so a fresh
        # responder separates an over-ceiling input from a merely premature one.
        over = ofca_native_snow.NoiseResponder(BRAIN_PRIVATE, AGENT_PUBLIC, prologue())
        with self.assertRaises(ofca_native_snow.NoiseInputError) as caught:
            over.encrypt_transport(b"x" * (AUTH_LIMIT + 2))
        self.assertEqual(caught.exception.args, ())
        at_limit = ofca_native_snow.NoiseResponder(BRAIN_PRIVATE, AGENT_PUBLIC, prologue())
        with self.assertRaises(ofca_native_snow.NoiseStateError):
            at_limit.encrypt_transport(b"x" * (AUTH_LIMIT + 1))

    def test_out_of_order_handshake_use_destroys_state(self) -> None:
        responder = ofca_native_snow.NoiseResponder(BRAIN_PRIVATE, AGENT_PUBLIC, prologue())
        with self.assertRaises(ofca_native_snow.NoiseStateError) as caught:
            responder.produce_responder_handshake()
        self.assertEqual(caught.exception.args, ())
        self.assertTrue(responder.closed())


if __name__ == "__main__":
    unittest.main()
