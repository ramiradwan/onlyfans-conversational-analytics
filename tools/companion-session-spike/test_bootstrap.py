"""Signed synthetic receipt to mutually authenticated Noise, without TOFU."""
from dataclasses import replace
import secrets
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from bootstrap import AUDIENCE, TYPE, Expectations, PairingGate, encode_key
from session import PROFILE, SessionError
from test_session import keypair, ready, SENSITIVE

NOW = 1800000000


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.authority = ec.generate_private_key(ec.SECP256R1())
        self.agent, agent_public = keypair()
        self.brain, brain_public = keypair()
        self.expected = Expectations(
            issuer="https://issuer.example.invalid", installation_id="installation-test",
            agent_id="agent-test", account_id="account-test",
            agent_nonce=secrets.token_hex(32), brain_nonce=secrets.token_hex(32),
            generation=1, grant_digest="a" * 64, local_public_key=agent_public,
            initiator=True, deadline=NOW + 300)
        self.claims = {
            "iss": self.expected.issuer, "aud": AUDIENCE, "iat": NOW, "exp": NOW + 300,
            "jti": secrets.token_hex(32), "suite": PROFILE.decode(),
            "installation_id": self.expected.installation_id,
            "agent_id": self.expected.agent_id, "account_id": self.expected.account_id,
            "agent_key": encode_key(agent_public), "brain_key": encode_key(brain_public),
            "agent_nonce": self.expected.agent_nonce, "brain_nonce": self.expected.brain_nonce,
            "generation": 1, "grant_digest": self.expected.grant_digest,
        }
        self.brain_expected = replace(self.expected, local_public_key=brain_public, initiator=False)

    def token(self, claims=None, key=None, headers=None):
        return jwt.encode(claims or self.claims, key or self.authority, algorithm="ES256",
                          headers=headers or {"typ": TYPE, "kid": "pairing-test"})

    def gate(self, expected=None):
        return PairingGate(expected or self.expected, {"pairing-test": self.authority.public_key()})

    def test_signed_receipt_drives_noise_on_both_endpoints(self):
        agent_gate, brain_gate = self.gate(), self.gate(self.brain_expected)
        token = self.token()
        agent_pins = agent_gate.accept(token, NOW)
        brain_pins = brain_gate.accept(token, NOW)
        self.assertEqual(agent_pins.binding, brain_pins.binding)
        agent = agent_gate.new_session(self.agent, NOW)
        brain = brain_gate.new_session(self.brain, NOW)
        ready(agent, brain)
        self.assertEqual(brain.unseal(agent.seal(SENSITIVE)), SENSITIVE)
        agent_gate.cancel_or_revoke()
        with self.assertRaises(SessionError):
            agent.seal(SENSITIVE)

    def test_wrong_authority_and_header_confusion(self):
        tokens = [self.token(key=ec.generate_private_key(ec.SECP256R1())),
                  self.token(headers={"typ": "JWT", "kid": "pairing-test"}),
                  self.token(headers={"typ": TYPE, "kid": "unknown"}),
                  self.token(headers={"typ": TYPE, "kid": "pairing-test", "jku": "https://attacker.invalid"})]
        for token in tokens:
            with self.subTest(token_kind=len(token)):
                with self.assertRaisesRegex(SessionError, "pairing_receipt_refused"):
                    self.gate().accept(token, NOW)

    def test_context_key_and_protocol_substitution(self):
        changes = {"installation_id": "other", "account_id": "other", "agent_id": "other",
                   "agent_nonce": "b" * 64, "brain_nonce": "c" * 64,
                   "generation": 2, "grant_digest": "d" * 64, "aud": "other",
                   "iss": "https://other.invalid", "agent_key": encode_key(keypair()[1]),
                   "suite": "Noise_NN_25519_ChaChaPoly_SHA256"}
        for name, value in changes.items():
            with self.subTest(field=name):
                with self.assertRaisesRegex(SessionError, "pairing_receipt_refused"):
                    self.gate().accept(self.token({**self.claims, name: value}), NOW)
        # Brain also refuses a receipt substituting its own key.
        with self.assertRaises(SessionError):
            self.gate(self.brain_expected).accept(self.token({**self.claims, "brain_key": encode_key(keypair()[1])}), NOW)

    def test_expiry_replay_cancel_and_private_key_substitution(self):
        for when in (NOW - 1, NOW + 300):
            with self.assertRaises(SessionError):
                self.gate().accept(self.token(), when)
        gate = self.gate()
        token = self.token()
        gate.accept(token, NOW)
        with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
            gate.accept(token, NOW)
        with self.assertRaises(SessionError):
            gate.new_session(self.agent, NOW + 300)
        with self.assertRaisesRegex(SessionError, "pairing_key_refused"):
            gate.new_session(keypair()[0], NOW)
        cancelled = self.gate()
        cancelled.cancel_or_revoke()
        with self.assertRaises(SessionError):
            cancelled.accept(token, NOW)

    def test_cancellation_during_verification_cannot_commit(self):
        gate = self.gate()
        decode = jwt.decode
        def cancelling_decode(*args, **kwargs):
            result = decode(*args, **kwargs)
            gate.cancel_or_revoke()
            return result
        with patch("bootstrap.jwt.decode", cancelling_decode):
            with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
                gate.accept(self.token(), NOW)
        with self.assertRaises(SessionError):
            gate.new_session(self.agent, NOW)

    def test_unpaired_gate_never_creates_session(self):
        with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
            self.gate().new_session(self.agent, NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
