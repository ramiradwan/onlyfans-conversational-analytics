"""Signed synthetic receipt to mutually authenticated Noise, without TOFU."""
from dataclasses import replace
import base64
import json
import secrets
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from bootstrap import (
    AUDIENCE, PAIRING_KEY_PURPOSE, TYPE, Expectations, IssuerKey, PairingGate,
    _P256_ORDER, encode_key, enrollment_digest, identity_proof_message,
    pairing_grant_digest, receipt_binding,
)
from session import PROFILE, SessionError
from test_session import keypair, ready, SENSITIVE

NOW = 1800000000


def b64u(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def raw_token(header, payload, key):
    header_segment = b64u(header if isinstance(header, bytes) else json.dumps(
        header, sort_keys=True, separators=(",", ":")).encode())
    payload_segment = b64u(payload if isinstance(payload, bytes) else json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    s = min(s, _P256_ORDER - s)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header_segment}.{payload_segment}.{b64u(signature)}"


def normalize_low_s(token):
    header, payload, signature = token.split(".")
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    s = min(s, _P256_ORDER - s)
    return f"{header}.{payload}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class Bootstrap(unittest.TestCase):
    def test_session_admission_rejects_preissuance_time_and_malformed_private_key(self):
        gate = self.gate()
        gate.accept(self.token(), NOW)
        for now in (-1, NOW - 1, True, float(NOW)):
            with self.subTest(now=now):
                with self.assertRaisesRegex(SessionError, "^pairing_state_refused$"):
                    gate.new_session(self.agent, now)
        for private in (b"short", None):
            with self.subTest(private=private):
                with self.assertRaisesRegex(SessionError, "^pairing_key_refused$"):
                    gate.new_session(private, NOW)
        gate.new_session(self.agent, NOW).close()

    def setUp(self):
        self.authority = ec.generate_private_key(ec.SECP256R1())
        self.agent, agent_public = keypair()
        self.brain, brain_public = keypair()
        self.expected = Expectations(
            issuer="https://issuer.example.invalid",
            organization_id="organization-test",
            installation_id="installation-test",
            installation_key_id="installation-key-test",
            installation_key_jkt=encode_key(b"i" * 32),
            agent_id="agent-test",
            agent_identity_key_id="agent-identity-key-test",
            agent_identity_key_jkt=encode_key(b"a" * 32),
            account_id="account-test",
            pairing_id="1" * 64,
            agent_nonce="2" * 64,
            brain_nonce="3" * 64,
            generation=1,
            grant_digest="4" * 64,
            approval_id="approval-test",
            approval_revision=1,
            offline_not_after=NOW + 3600,
            local_public_key=agent_public,
            initiator=True,
            deadline=NOW + 300,
        )
        self.claims = {
            "iss": self.expected.issuer, "aud": AUDIENCE,
            "iat": NOW, "exp": NOW + 300,
            "jti": "5" * 64, "suite": PROFILE.decode(),
            "organization_id": self.expected.organization_id,
            "installation_id": self.expected.installation_id,
            "installation_key_id": self.expected.installation_key_id,
            "installation_key_jkt": self.expected.installation_key_jkt,
            "agent_id": self.expected.agent_id,
            "agent_identity_key_id": self.expected.agent_identity_key_id,
            "agent_identity_key_jkt": self.expected.agent_identity_key_jkt,
            "account_id": self.expected.account_id,
            "pairing_id": self.expected.pairing_id,
            "agent_key": encode_key(agent_public), "brain_key": encode_key(brain_public),
            "agent_nonce": self.expected.agent_nonce, "brain_nonce": self.expected.brain_nonce,
            "generation": self.expected.generation,
            "grant_digest": self.expected.grant_digest,
            "approval_id": self.expected.approval_id,
            "approval_revision": self.expected.approval_revision,
            "offline_not_after": self.expected.offline_not_after,
        }
        self.brain_expected = replace(self.expected, local_public_key=brain_public, initiator=False)

    def token(self, claims=None, key=None, headers=None):
        token = jwt.encode(
            claims or self.claims, key or self.authority, algorithm="ES256",
            headers=headers or {"typ": TYPE, "kid": "pairing-test"},
        )
        return normalize_low_s(token)

    def gate(self, expected=None, **state):
        return PairingGate(
            expected or self.expected,
            {"pairing-test": IssuerKey(PAIRING_KEY_PURPOSE, self.authority.public_key())},
            **state,
        )

    def test_signed_receipt_drives_noise_on_both_endpoints(self):
        agent_gate, brain_gate = self.gate(), self.gate(self.brain_expected)
        token = self.token()
        agent_pins = agent_gate.accept(token, NOW)
        brain_pins = brain_gate.accept(token, NOW)
        self.assertEqual(agent_pins.binding, brain_pins.binding)
        self.assertEqual(agent_pins.generation, 1)
        self.assertEqual(agent_pins.issuer_kid, "pairing-test")
        agent = agent_gate.new_session(self.agent, NOW)
        brain = brain_gate.new_session(self.brain, NOW)
        ready(agent, brain)
        self.assertEqual(brain.unseal(agent.seal(SENSITIVE)), SENSITIVE)
        agent_gate.cancel_or_revoke()
        with self.assertRaises(SessionError):
            agent.seal(SENSITIVE)

    def test_wrong_authority_wrong_purpose_and_header_confusion(self):
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        tokens = [
            self.token(key=wrong_key),
            self.token(headers={"typ": "JWT", "kid": "pairing-test"}),
            self.token(headers={"typ": TYPE, "kid": "unknown"}),
            self.token(headers={"typ": TYPE, "kid": "pairing-test", "jku": "https://attacker.invalid"}),
        ]
        for token in tokens:
            with self.subTest(token_kind=len(token)):
                with self.assertRaisesRegex(SessionError, "pairing_receipt_refused"):
                    self.gate().accept(token, NOW)
        wrong_purpose = PairingGate(
            self.expected,
            {"pairing-test": IssuerKey("installation-binding", self.authority.public_key())},
        )
        with self.assertRaises(SessionError):
            wrong_purpose.accept(self.token(), NOW)

    def test_context_identity_key_and_protocol_substitution(self):
        changes = {
            "organization_id": "other", "installation_id": "other",
            "installation_key_id": "other", "installation_key_jkt": encode_key(b"j" * 32),
            "account_id": "other", "agent_id": "other",
            "agent_identity_key_id": "other", "agent_identity_key_jkt": encode_key(b"b" * 32),
            "pairing_id": "6" * 64, "agent_nonce": "7" * 64,
            "brain_nonce": "8" * 64, "generation": 2,
            "grant_digest": "9" * 64, "approval_id": "other-approval",
            "approval_revision": 2, "offline_not_after": NOW + 7200,
            "aud": "other", "iss": "https://other.invalid",
            "agent_key": encode_key(keypair()[1]),
            "suite": "Noise_NN_25519_ChaChaPoly_SHA256",
        }
        for name, value in changes.items():
            with self.subTest(field=name):
                with self.assertRaisesRegex(SessionError, "pairing_receipt_refused"):
                    self.gate().accept(self.token({**self.claims, name: value}), NOW)
        with self.assertRaises(SessionError):
            self.gate(self.brain_expected).accept(
                self.token({**self.claims, "brain_key": encode_key(keypair()[1])}), NOW
            )

    def test_closed_jws_profile_rejects_duplicates_extra_claims_and_high_s(self):
        extra = self.token({**self.claims, "unexpected": "value"})
        with self.assertRaises(SessionError):
            self.gate().accept(extra, NOW)

        header = {"alg": "ES256", "kid": "pairing-test", "typ": TYPE}
        payload = json.dumps(self.claims, sort_keys=True, separators=(",", ":")).encode()
        duplicate_payload = payload[:-1] + b',"iss":"https://issuer.example.invalid"}'
        with self.assertRaises(SessionError):
            self.gate().accept(raw_token(header, duplicate_payload, self.authority), NOW)

        duplicate_header = (
            b'{"alg":"ES256","kid":"pairing-test","typ":"' + TYPE.encode()
            + b'","kid":"pairing-test"}'
        )
        with self.assertRaises(SessionError):
            self.gate().accept(raw_token(duplicate_header, self.claims, self.authority), NOW)

        low = self.token()
        h, p, s = low.split(".")
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        r = int.from_bytes(raw[:32], "big")
        low_s = int.from_bytes(raw[32:], "big")
        high = r.to_bytes(32, "big") + (_P256_ORDER - low_s).to_bytes(32, "big")
        with self.assertRaises(SessionError):
            self.gate().accept(f"{h}.{p}.{b64u(high)}", NOW)

        with self.assertRaises(SessionError):
            self.gate().accept(low + "=", NOW)

    def test_receipt_expiry_replay_rollback_cancel_and_private_key_substitution(self):
        for when in (NOW - 1, NOW + 300):
            with self.assertRaises(SessionError):
                self.gate().accept(self.token(), when)

        gate = self.gate()
        token = self.token()
        gate.accept(token, NOW)
        with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
            gate.accept(token, NOW)
        highest, revoked, consumed = gate.durable_replay_state()
        self.assertEqual((highest, revoked), (1, 0))

        restarted = self.gate(highest_generation=highest, consumed_receipts=consumed)
        with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
            restarted.accept(token, NOW)

        # Receipt expiry only bounds admission. An admitted pin may operate until
        # its separately authorized offline deadline.
        gate.new_session(self.agent, NOW + 301)
        with self.assertRaises(SessionError):
            gate.new_session(self.agent, NOW + 3600)
        with self.assertRaisesRegex(SessionError, "pairing_key_refused"):
            gate.new_session(keypair()[0], NOW + 302)

        cancelled = self.gate()
        cancelled.cancel_or_revoke()
        with self.assertRaises(SessionError):
            cancelled.accept(token, NOW)
        _, revoked, _ = cancelled.durable_replay_state()
        self.assertEqual(revoked, 1)

    def test_higher_generation_requires_fresh_pending_context(self):
        old_gate = self.gate()
        old_gate.accept(self.token(), NOW)
        highest, _, consumed = old_gate.durable_replay_state()

        expected2 = replace(
            self.expected, generation=2, agent_nonce="a" * 64,
            brain_nonce="b" * 64, approval_revision=2,
        )
        claims2 = {
            **self.claims, "jti": "c" * 64, "generation": 2,
            "agent_nonce": expected2.agent_nonce, "brain_nonce": expected2.brain_nonce,
            "approval_revision": 2,
        }
        replacement = self.gate(
            expected2, highest_generation=highest, consumed_receipts=consumed
        )
        replacement.accept(self.token(claims2), NOW)

        stale_context = replace(expected2, agent_nonce=self.expected.agent_nonce)
        with self.assertRaises(SessionError):
            self.gate(stale_context, highest_generation=highest).accept(self.token(claims2), NOW)

        revoked = self.gate(expected2, revoked_through_generation=2)
        with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
            revoked.accept(self.token(claims2), NOW)

    def test_cancellation_during_verification_cannot_commit(self):
        gate = self.gate()
        from bootstrap import _verify_es256

        def cancelling_verify(*args, **kwargs):
            result = _verify_es256(*args, **kwargs)
            gate.cancel_or_revoke()
            return result

        with patch("bootstrap._verify_es256", cancelling_verify):
            with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
                gate.accept(self.token(), NOW)
        with self.assertRaises(SessionError):
            gate.new_session(self.agent, NOW)

    def test_enrollment_and_identity_proof_transcripts_are_domain_and_role_bound(self):
        digest = enrollment_digest(self.claims)
        self.assertEqual(len(digest), 32)
        changed = enrollment_digest({**self.claims, "brain_key": encode_key(keypair()[1])})
        self.assertNotEqual(digest, changed)
        challenge = bytes.fromhex("d" * 64)
        agent = identity_proof_message(
            "agent", challenge, digest, self.expected.agent_identity_key_id
        )
        brain = identity_proof_message(
            "brain", challenge, digest, self.expected.installation_key_id
        )
        self.assertNotEqual(agent, brain)
        self.assertNotEqual(
            agent,
            identity_proof_message(
                "agent", bytes.fromhex("e" * 64), digest,
                self.expected.agent_identity_key_id,
            ),
        )

    def test_noise_binding_has_a_fixed_cross_runtime_vector(self):
        vector = {
            "iss": "https://pairing.example", "aud": AUDIENCE,
            "iat": 1800000000, "exp": 1800000300, "jti": "01" * 32,
            "suite": PROFILE.decode(), "organization_id": "org-1",
            "installation_id": "installation-1", "installation_key_id": "ik-1",
            "installation_key_jkt": encode_key(bytes(range(32))),
            "agent_id": "agent-1", "agent_identity_key_id": "ak-1",
            "agent_identity_key_jkt": encode_key(bytes(range(32, 64))),
            "account_id": "account-1", "pairing_id": "02" * 32,
            "agent_key": encode_key(bytes(range(64, 96))),
            "brain_key": encode_key(bytes(range(96, 128))),
            "agent_nonce": "03" * 32, "brain_nonce": "04" * 32,
            "generation": 7, "grant_digest": "05" * 32,
            "approval_id": "approval-7", "approval_revision": 3,
            "offline_not_after": 1800086400,
        }
        digest = enrollment_digest(vector)
        self.assertEqual(
            digest.hex(),
            "bccd863b01ea9e140c9827fa07491801aa1afd1896f477584f14318c72da8d88",
        )
        self.assertEqual(
            receipt_binding(vector).hex(),
            "a3c1caeb43855787de35bf7081d5b70912bdd2027d27be1bdf4843eb86befe61",
        )
        self.assertEqual(
            identity_proof_message("agent", bytes.fromhex("06" * 32), digest, "ak-1").hex(),
            "4f4643412d434f4d50414e494f4e2d50414952494e472d50524f4f462d563100"
            "0000001270616972696e672d656e726f6c6c6d656e740000001d75726e3a6f6663"
            "613a636f6d70616e696f6e2d70616972696e673a7631000000056167656e740000"
            "002006060606060606060606060606060606060606060606060606060606060606"
            "0600000020bccd863b01ea9e140c9827fa07491801aa1afd1896f477584f14318c"
            "72da8d8800000004616b2d31",
        )
        self.assertEqual(
            pairing_grant_digest({
                "creator_account_binding": "aa" * 32,
                "installation_grant": "bb" * 32,
            }),
            "bfa4e2f5cbb27ece8258150d6644b19b1fbb754a957f63f7ccc1ed4fc6f30f10",
        )

    def test_unpaired_gate_never_creates_session(self):
        with self.assertRaisesRegex(SessionError, "pairing_state_refused"):
            self.gate().new_session(self.agent, NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
