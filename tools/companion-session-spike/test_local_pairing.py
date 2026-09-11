"""Local pairing reference: vector reproduction, checks, window, and session."""
from dataclasses import fields, replace
import json
import secrets
import unittest

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

import local_pairing as lp
from session import PROLOGUE, Session, SessionError
from test_session import ready

VECTOR = json.loads(lp.VECTOR_PATH.read_text(encoding="utf-8"))


def offer_check(case):
    return lp.verify_offer(
        VECTOR["request"], lp.case_frame(case), trust_set=VECTOR["trust_set"],
        detected_account_id=VECTOR["detected_account_id"],
        high_water=case.get("high_water", {}), now=VECTOR["now"],
    )


class Vector(unittest.TestCase):
    def test_committed_vector_is_reproduced(self):
        self.assertEqual(lp.build_vector(), VECTOR)

    def test_session_prologue_matches_the_session_profile(self):
        self.assertEqual(lp.SESSION_PROFILE, PROLOGUE)

    def test_positive_vector_verifies(self):
        verified = offer_check({"text": lp.encode_message(VECTOR["offer"])})
        expected = VECTOR["expected"]
        self.assertEqual(verified.pairing_digest.hex(), expected["pairing_digest"])
        self.assertEqual(verified.grant_digest.hex(), expected["grant_digest"])
        self.assertEqual(verified.comparison_code, expected["comparison_code"])
        self.assertEqual(verified.grants_not_after, expected["grants_not_after"])
        self.assertEqual(
            lp.session_prologue(verified.pairing_digest).hex(), expected["session_prologue"]
        )
        lp.verify_confirm(
            lp.encode_message(VECTOR["confirm"]), pairing_id=verified.pairing_id,
            agent_jwk=VECTOR["request"]["agent_identity_jwk"],
            digest=verified.pairing_digest,
        )

    def test_every_negative_case_is_refused_with_its_code(self):
        for case in VECTOR["offer_cases"]:
            with self.subTest(case=case["name"]):
                with self.assertRaises(lp.PairingError) as caught:
                    offer_check(case)
                self.assertEqual(
                    (caught.exception.code, caught.exception.detail),
                    (case["error"], case["detail"]),
                )
        brain_key = X25519PrivateKey.from_private_bytes(
            bytes.fromhex(VECTOR["test_keys"]["brain_noise_private"])
        ).public_key().public_bytes_raw()
        for case in VECTOR["request_cases"]:
            with self.subTest(request=case["name"]):
                with self.assertRaisesRegex(lp.PairingError, f"^{case['error']}$"):
                    lp.verify_request(
                        case["text"], brain_noise_key=brain_key,
                        brain_nonce=lp.unb64u(VECTOR["offer"]["brain_nonce"]),
                    )
        digest = bytes.fromhex(VECTOR["expected"]["pairing_digest"])
        for case in VECTOR["confirm_cases"]:
            with self.subTest(confirm=case["name"]):
                with self.assertRaisesRegex(lp.PairingError, f"^{case['error']}$"):
                    lp.verify_confirm(
                        case["text"], pairing_id=lp.unb64u(VECTOR["offer"]["pairing_id"]),
                        agent_jwk=VECTOR["request"]["agent_identity_jwk"], digest=digest,
                    )

    def test_authorization_cases(self):
        identity = lp.GrantIdentity(**VECTOR["expected"]["identity"])
        for case in VECTOR["authorization_cases"]:
            with self.subTest(case=case["name"]):
                if "error" in case:
                    with self.assertRaises(lp.PairingError) as caught:
                        lp.verify_session_authorization(
                            case["text"], identity, trust_set=VECTOR["trust_set"], now=case["now"]
                        )
                    self.assertEqual(
                        (caught.exception.code, caught.exception.detail),
                        (case["error"], case["detail"]),
                    )
                else:
                    self.assertEqual(lp.verify_session_authorization(
                        case["text"], identity, trust_set=VECTOR["trust_set"], now=case["now"]
                    ), case["not_after"])

    def test_every_transcript_field_changes_the_digest(self):
        request, offer = VECTOR["request"], VECTOR["offer"]
        identity = lp.GrantIdentity(**VECTOR["expected"]["identity"])
        base = lp.transcript_of(
            request, offer, identity, bytes.fromhex(VECTOR["expected"]["grant_digest"])
        )
        self.assertEqual(lp.pairing_transcript(base).hex(), VECTOR["expected"]["transcript"])
        for field in fields(base):
            value = getattr(base, field.name)
            changed = (
                value + 1 if isinstance(value, int)
                else value + "x" if isinstance(value, str)
                else bytes([value[0] ^ 1]) + value[1:]
            )
            with self.subTest(field=field.name):
                self.assertNotEqual(
                    lp.pairing_digest(replace(base, **{field.name: changed})),
                    lp.pairing_digest(base),
                )

    def test_small_order_points_have_all_zero_shared_secrets(self):
        private = X25519PrivateKey.generate()
        for point in lp.SMALL_ORDER_POINTS:
            for encoded in (point, point[:31] + bytes([point[31] | 0x80])):
                with self.subTest(point=encoded.hex()):
                    self.assertTrue(lp.is_small_order(encoded))
                    with self.assertRaises(ValueError):
                        private.exchange(X25519PublicKey.from_public_bytes(encoded))
        for _ in range(64):
            key = X25519PrivateKey.generate().public_key().public_bytes_raw()
            self.assertFalse(lp.is_small_order(key))
            private.exchange(X25519PublicKey.from_public_bytes(key))
        self.assertEqual([p.hex() for p in lp.SMALL_ORDER_POINTS], VECTOR["small_order_points"])

    def test_transcript_integers_and_proof_roles_are_bounded(self):
        with self.assertRaises(ValueError):
            lp.proof_message("bridge", bytes(32))
        with self.assertRaises(ValueError):
            lp.proof_message("brain", bytes(31))
        with self.assertRaises(ValueError):
            lp.session_prologue(bytes(33))


class Window(unittest.TestCase):
    def setUp(self):
        self.f = lp._Fixture()
        self.window = lp.PairingWindow(
            opened_at=lp.NOW, generation=2, pairing_id=secrets.token_bytes(32),
            creator_account_id=lp.ACCOUNT_ID,
            brain_noise_private=secrets.token_bytes(32),
            brain_nonce=secrets.token_bytes(32), installation_key=self.f.installation,
            grants=self.f.base_grants,
        )
        self.agent_noise = secrets.token_bytes(32)
        self.request = self.f.request(
            agent_noise_key=lp.b64u(lp._x25519_public(self.agent_noise)),
            agent_nonce=lp.b64u(secrets.token_bytes(32)),
        )

    def pair(self, high_water=None):
        offer = self.window.offer(lp.encode_message(self.request), lp.NOW + 1)
        verified = lp.verify_offer(
            self.request, lp.encode_message(offer), trust_set=self.f.trust_set(),
            detected_account_id=lp.ACCOUNT_ID, high_water=high_water or {}, now=lp.NOW + 1,
        )
        confirm = {
            "type": "pair.confirm", "pairing_id": offer["pairing_id"],
            "agent_proof": lp.sign_proof(self.f.agent_identity, "agent", verified.pairing_digest),
        }
        return offer, verified, confirm

    def test_confirmed_pairing_opens_a_session_under_its_digest(self):
        offer, verified, confirm = self.pair()
        code = self.window.confirm(lp.encode_message(confirm), lp.NOW + 2)
        self.assertEqual(code, verified.comparison_code)
        result = self.window.decide(True, lp.NOW + 3)
        self.assertEqual(lp.parse_message(lp.encode_message(result), "pair.result")["outcome"], "confirmed")
        agent = Session(self.agent_noise, verified.brain_noise_key, True, verified.pairing_digest)
        brain = Session(
            self.window.brain_noise_private, lp.unb64u(self.request["agent_noise_key"]),
            False, self.window.digest,
        )
        ready(agent, brain)
        self.assertEqual(brain.unseal(agent.seal(b"record")), b"record")

    def test_session_under_another_pairing_digest_fails(self):
        _, verified, confirm = self.pair()
        self.window.confirm(lp.encode_message(confirm), lp.NOW + 2)
        agent = Session(self.agent_noise, verified.brain_noise_key, True, verified.pairing_digest)
        brain = Session(
            self.window.brain_noise_private, lp.unb64u(self.request["agent_noise_key"]),
            False, bytes(32),
        )
        with self.assertRaisesRegex(SessionError, "handshake_failed"):
            brain.handshake_read(agent.handshake_write())

    def test_second_request_cancels_the_window(self):
        self.window.offer(lp.encode_message(self.request), lp.NOW + 1)
        with self.assertRaisesRegex(lp.PairingError, "pairing_state_refused"):
            self.window.offer(lp.encode_message(self.request), lp.NOW + 2)
        self.assertEqual(self.window.state, "cancelled")
        with self.assertRaisesRegex(lp.PairingError, "pairing_state_refused"):
            self.window.confirm("{}", lp.NOW + 3)

    def test_expired_window_refuses_each_step(self):
        with self.assertRaisesRegex(lp.PairingError, "pairing_state_refused"):
            self.window.offer(lp.encode_message(self.request), lp.NOW + lp.WINDOW_SECONDS)
        self.assertEqual(self.window.state, "expired")

    def test_expiry_before_operator_decision_refuses_commit(self):
        _, _, confirm = self.pair()
        self.window.confirm(lp.encode_message(confirm), lp.NOW + 2)
        with self.assertRaisesRegex(lp.PairingError, "pairing_state_refused"):
            self.window.decide(True, lp.NOW + lp.WINDOW_SECONDS)
        self.assertEqual(self.window.state, "expired")

    def test_decision_requires_a_verified_agent_proof(self):
        self.window.offer(lp.encode_message(self.request), lp.NOW + 1)
        with self.assertRaisesRegex(lp.PairingError, "pairing_state_refused"):
            self.window.decide(True, lp.NOW + 2)
        self.assertEqual(self.window.state, "cancelled")

    def test_bad_agent_proof_cancels_the_window(self):
        _, verified, confirm = self.pair()
        confirm["agent_proof"] = lp.sign_proof(self.f.other_installation, "agent", verified.pairing_digest)
        with self.assertRaisesRegex(lp.PairingError, "pairing_proof_refused"):
            self.window.confirm(lp.encode_message(confirm), lp.NOW + 2)
        self.assertEqual(self.window.state, "cancelled")

    def test_agent_refuses_a_generation_it_has_admitted(self):
        with self.assertRaisesRegex(lp.PairingError, "pairing_generation_refused"):
            self.pair(high_water={lp.INSTALLATION_ID: 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
