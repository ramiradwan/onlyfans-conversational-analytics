"""Reference model of local Agent-to-Brain pairing.

Implements the vendored companion-pairing contract: the transcript, proofs,
comparison code, session prologue, message schemas, and the Agent and Brain
checks. Brain's window is modelled in memory; production Brain keeps it in
auth.sqlite3 and signs through the installation-key provider.

The published vector is not generated here. load_contract() reads the copy
vendored under contracts/ and returns it only when every byte matches the
contract manifest and the independent consumer pin.
"""

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.security.grant_verifier import (  # noqa: E402
    GrantVerificationContext,
    verify_grant,
)

SUITE = "Noise_KK_25519_ChaChaPoly_SHA256"
SESSION_PROFILE = b"ofca-companion-session/v1;agent-to-brain;no-early-data"
PAIRING_PATH = "/ws/agent/pairing"
MAX_FRAME_BYTES = 36_864
MAX_GRANT_LENGTH = 16_384
NOISE_TAG_BYTES = 16
RECORD_DISCRIMINATOR_BYTES = 1
RECORD_OVERHEAD_BYTES = NOISE_TAG_BYTES + RECORD_DISCRIMINATOR_BYTES
MAX_APPLICATION_FRAME_BYTES = 4_096
MAX_APPLICATION_RECORD_PLAINTEXT_BYTES = (
    MAX_APPLICATION_FRAME_BYTES - RECORD_OVERHEAD_BYTES
)
MAX_AUTHORIZATION_RECORD_PLAINTEXT_BYTES = MAX_FRAME_BYTES - RECORD_OVERHEAD_BYTES
WINDOW_SECONDS = 300
CONTRACT_EXPORT = "companion-pairing-v1"
CONTRACT_PROFILE = "urn:bridge-clean:companion-pairing:v1"
FIXTURE_LABEL_PREFIX = "OFCA TEST VECTORS ONLY - NEVER PRODUCTION - "

_GRANTS_DOMAIN = b"OFCA-LOCAL-PAIRING-GRANTS-V1\x00"
_TRANSCRIPT_DOMAIN = b"OFCA-LOCAL-PAIRING-TRANSCRIPT-V1\x00"
_PROOF_DOMAIN = b"OFCA-LOCAL-PAIRING-PROOF-V1\x00"
_CODE_DOMAIN = b"OFCA-LOCAL-PAIRING-CODE-V1\x00"

GRANT_AUDIENCES = {
    "installation_grant": "urn:bridge-clean:local-brain:installation",
    "creator_account_binding": "urn:bridge-clean:local-brain:creator-binding",
}
GRANT_GRACE = {"installation_grant": 604_800, "creator_account_binding": 259_200}
_GRANT_LIFETIME = {"installation_grant": 2_592_000, "creator_account_binding": 604_800}
_GRANT_TYP = {
    "installation_grant": "urn:bridge-clean:grant:installation:v1",
    "creator_account_binding": "urn:bridge-clean:grant:creator-binding:v1",
}

# libsodium's X25519 small-order encodings, compared with the top bit cleared.
SMALL_ORDER_POINTS = tuple(bytes.fromhex(value) for value in (
    "00" * 32,
    "01" + "00" * 31,
    "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
    "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
    "ec" + "ff" * 30 + "7f",
    "ed" + "ff" * 30 + "7f",
    "ee" + "ff" * 30 + "7f",
))

_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_JWS_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
OUTCOMES = ("confirmed", "declined", "expired", "cancelled")

MESSAGE_LIMITS = {
    "pair.request": MAX_FRAME_BYTES,
    "pair.offer": MAX_FRAME_BYTES,
    "pair.confirm": MAX_FRAME_BYTES,
    "pair.result": MAX_FRAME_BYTES,
    "session.authorization": MAX_AUTHORIZATION_RECORD_PLAINTEXT_BYTES,
}

SCHEMAS = {
    "pair.request": {
        "agent_installation_id": "id", "agent_identity_jwk": "jwk",
        "agent_noise_key": "key", "agent_nonce": "key",
    },
    "pair.offer": {
        "pairing_id": "key", "generation": "generation",
        "creator_account_id": "id", "brain_noise_key": "key",
        "brain_nonce": "key", "installation_jwk": "jwk",
        "installation_grant": "grant", "creator_account_binding": "grant",
        "brain_proof": "signature",
    },
    "pair.confirm": {"pairing_id": "key", "agent_proof": "signature"},
    "pair.result": {"pairing_id": "key", "outcome": "outcome"},
    "session.authorization": {
        "installation_grant": "grant", "creator_account_binding": "grant",
    },
}


class PairingError(Exception):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code} {detail}")


def b64u(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64u(value):
    if not isinstance(value, str) or not _B64U_RE.fullmatch(value):
        raise ValueError("noncanonical base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("noncanonical base64url") from exc
    if b64u(raw) != value:
        raise ValueError("noncanonical base64url")
    return raw


def is_small_order(key):
    return key[:31] + bytes([key[31] & 0x7F]) in SMALL_ORDER_POINTS


def _u64(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("invalid integer")
    return struct.pack("!Q", value)


def _lp(domain, fields):
    result = bytearray(domain)
    for field in fields:
        if isinstance(field, str):
            field = field.encode("utf-8")
        elif type(field) is int:
            field = _u64(field)
        elif not isinstance(field, bytes):
            raise TypeError("invalid canonical field")
        result += struct.pack("!I", len(field)) + field
    return bytes(result)


def _sha256(data):
    return hashlib.sha256(data).digest()


def grant_digest(creator_account_binding, installation_grant):
    return _sha256(_lp(_GRANTS_DOMAIN, (
        "creator_account_binding", _sha256(creator_account_binding.encode("ascii")),
        "installation_grant", _sha256(installation_grant.encode("ascii")),
    )))


@dataclass(frozen=True)
class Transcript:
    pairing_id: bytes
    generation: int
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    creator_account_id: str
    agent_installation_id: str
    agent_identity_key_jkt: str
    agent_noise_key: bytes
    brain_noise_key: bytes
    agent_nonce: bytes
    brain_nonce: bytes
    grant_digest: bytes


def pairing_transcript(t):
    return _lp(_TRANSCRIPT_DOMAIN, (
        SUITE, t.pairing_id, t.generation, t.organization_id, t.installation_id,
        t.installation_key_id, t.installation_key_jkt, t.creator_account_id,
        t.agent_installation_id, t.agent_identity_key_jkt, t.agent_noise_key,
        t.brain_noise_key, t.agent_nonce, t.brain_nonce, t.grant_digest,
    ))


def pairing_digest(t):
    return _sha256(pairing_transcript(t))


def proof_message(role, digest):
    if role not in ("agent", "brain") or len(digest) != 32:
        raise ValueError("invalid proof input")
    return _lp(_PROOF_DOMAIN, (role, digest))


def comparison_code(digest):
    value = int.from_bytes(_sha256(_lp(_CODE_DOMAIN, (digest,)))[:4], "big")
    return f"{value % 1_000_000:06d}"


def session_prologue(digest):
    if len(digest) != 32:
        raise ValueError("invalid pairing digest")
    return SESSION_PROFILE + b"\x00" + digest


def public_jwk(key):
    numbers = key.public_key().public_numbers() if hasattr(key, "public_key") else key.public_numbers()
    return {
        "crv": "P-256", "kty": "EC",
        "x": b64u(numbers.x.to_bytes(32, "big")),
        "y": b64u(numbers.y.to_bytes(32, "big")),
    }


def jwk_thumbprint(jwk):
    bare = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    return b64u(_sha256(json.dumps(bare, sort_keys=True, separators=(",", ":")).encode()))


def jwk_public_key(jwk):
    if not isinstance(jwk, dict) or set(jwk) != {"crv", "kty", "x", "y"}:
        raise ValueError("invalid jwk")
    if jwk["crv"] != "P-256" or jwk["kty"] != "EC":
        raise ValueError("invalid jwk")
    x, y = unb64u(jwk["x"]), unb64u(jwk["y"])
    if len(x) != 32 or len(y) != 32:
        raise ValueError("invalid jwk")
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
    ).public_key()


def sign_proof(private_key, role, digest):
    der = private_key.sign(
        proof_message(role, digest),
        ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
    )
    r, s = decode_dss_signature(der)
    s = min(s, _P256_ORDER - s)
    return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def verify_proof(jwk, role, digest, signature):
    try:
        raw = unb64u(signature)
        if len(raw) != 64:
            return False
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        if not 1 <= r < _P256_ORDER or not 1 <= s <= _P256_ORDER // 2:
            return False
        jwk_public_key(jwk).verify(
            encode_dss_signature(r, s), proof_message(role, digest),
            ec.ECDSA(hashes.SHA256()),
        )
    except (ValueError, InvalidSignature):
        return False
    return True


def _reject_constant(_):
    raise ValueError("unsupported json value")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _valid_field(kind, value):
    if kind == "id":
        return isinstance(value, str) and _ID_RE.fullmatch(value) is not None
    if kind == "key":
        return isinstance(value, str) and len(value) == 43 and len(unb64u(value)) == 32
    if kind == "signature":
        return isinstance(value, str) and len(value) == 86 and len(unb64u(value)) == 64
    if kind == "jwk":
        jwk_public_key(value)
        return True
    if kind == "grant":
        return (
            isinstance(value, str)
            and len(value) <= MAX_GRANT_LENGTH
            and _JWS_RE.fullmatch(value) is not None
        )
    if kind == "generation":
        return type(value) is int and 1 <= value < 2**53
    return value in OUTCOMES


def parse_message(data, message_type):
    """Parse one pairing message under its closed schema and record bound."""
    try:
        if isinstance(data, str):
            data = data.encode("utf-8")
        if len(data) > MESSAGE_LIMITS[message_type]:
            raise ValueError("frame too large")
        message = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique,
            parse_float=_reject_constant, parse_constant=_reject_constant,
        )
        schema = SCHEMAS[message_type]
        if not isinstance(message, dict) or message.get("type") != message_type:
            raise ValueError("wrong message type")
        if set(message) != {"type", *schema}:
            raise ValueError("schema mismatch")
        for name, kind in schema.items():
            if not _valid_field(kind, message[name]):
                raise ValueError("invalid member")
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise PairingError("pairing_message_invalid") from None
    return message


def encode_message(message):
    return json.dumps(message, separators=(",", ":"))


def _peek(token):
    try:
        payload = json.loads(
            unb64u(token.split(".")[1]).decode("utf-8"), object_pairs_hook=_unique
        )
    except (ValueError, UnicodeError, IndexError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _text(claims, name):
    value = claims.get(name)
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class GrantIdentity:
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    creator_account_id: str


def verify_grants(installation_grant, creator_account_binding, identity, *, trust_set, now):
    """Verify both Brain-audience grants for one identity; return their joint limit."""
    not_after = None
    for grant_type, token in (
        ("installation_grant", installation_grant),
        ("creator_account_binding", creator_account_binding),
    ):
        subject = f"installation:{identity.installation_id}"
        if grant_type == "creator_account_binding":
            subject += f":creator:{identity.creator_account_id}"
        outcome = verify_grant(token, context=GrantVerificationContext(
            expected_grant_type=grant_type,
            expected_audience=GRANT_AUDIENCES[grant_type],
            expected_organization_id=identity.organization_id,
            expected_installation_id=identity.installation_id,
            expected_installation_key_id=identity.installation_key_id,
            expected_installation_key_jkt=identity.installation_key_jkt,
            expected_subject=subject,
            verifier_time=now,
        ), trust_set=trust_set)
        if not outcome.valid:
            raise PairingError("pairing_grant_refused", f"{grant_type}:{outcome.result}")
        claims = _peek(token)
        limit = claims["exp"] + GRANT_GRACE[grant_type]
        not_after = limit if not_after is None else min(not_after, limit)
    if _peek(creator_account_binding).get("creator_account_id") != identity.creator_account_id:
        raise PairingError("pairing_account_refused")
    return not_after


@dataclass(frozen=True)
class VerifiedOffer:
    identity: GrantIdentity
    pairing_id: bytes
    generation: int
    brain_noise_key: bytes
    grant_digest: bytes
    pairing_digest: bytes
    comparison_code: str
    grants_not_after: int


def verify_offer(request, data, *, trust_set, detected_account_id, high_water, now):
    """Agent checks for pair.offer, in the order the contract lists them."""
    offer = parse_message(data, "pair.offer")
    agent_noise_key = unb64u(request["agent_noise_key"])
    brain_noise_key = unb64u(offer["brain_noise_key"])
    if is_small_order(brain_noise_key) or brain_noise_key == agent_noise_key:
        raise PairingError("pairing_key_refused")
    if offer["brain_nonce"] == request["agent_nonce"]:
        raise PairingError("pairing_nonce_refused")
    claims = _peek(offer["installation_grant"])
    identity = GrantIdentity(
        _text(claims, "organization_id"), _text(claims, "installation_id"),
        _text(claims, "installation_key_id"), jwk_thumbprint(offer["installation_jwk"]),
        offer["creator_account_id"],
    )
    not_after = verify_grants(
        offer["installation_grant"], offer["creator_account_binding"], identity,
        trust_set=trust_set, now=now,
    )
    if offer["creator_account_id"] != detected_account_id:
        raise PairingError("pairing_account_refused")
    if offer["generation"] <= high_water.get(identity.installation_id, 0):
        raise PairingError("pairing_generation_refused")
    grants = grant_digest(offer["creator_account_binding"], offer["installation_grant"])
    digest = pairing_digest(transcript_of(request, offer, identity, grants))
    if not verify_proof(offer["installation_jwk"], "brain", digest, offer["brain_proof"]):
        raise PairingError("pairing_proof_refused")
    return VerifiedOffer(
        identity, unb64u(offer["pairing_id"]), offer["generation"], brain_noise_key,
        grants, digest, comparison_code(digest), not_after,
    )


def transcript_of(request, offer, identity, grants):
    return Transcript(
        pairing_id=unb64u(offer["pairing_id"]),
        generation=offer["generation"],
        organization_id=identity.organization_id,
        installation_id=identity.installation_id,
        installation_key_id=identity.installation_key_id,
        installation_key_jkt=identity.installation_key_jkt,
        creator_account_id=identity.creator_account_id,
        agent_installation_id=request["agent_installation_id"],
        agent_identity_key_jkt=jwk_thumbprint(request["agent_identity_jwk"]),
        agent_noise_key=unb64u(request["agent_noise_key"]),
        brain_noise_key=unb64u(offer["brain_noise_key"]),
        agent_nonce=unb64u(request["agent_nonce"]),
        brain_nonce=unb64u(offer["brain_nonce"]),
        grant_digest=grants,
    )


def verify_request(data, *, brain_noise_key, brain_nonce):
    """Brain checks for pair.request."""
    request = parse_message(data, "pair.request")
    agent_noise_key = unb64u(request["agent_noise_key"])
    if is_small_order(agent_noise_key) or agent_noise_key == brain_noise_key:
        raise PairingError("pairing_key_refused")
    if unb64u(request["agent_nonce"]) == brain_nonce:
        raise PairingError("pairing_nonce_refused")
    return request


def verify_confirm(data, *, pairing_id, agent_jwk, digest):
    """Brain checks for pair.confirm."""
    confirm = parse_message(data, "pair.confirm")
    if unb64u(confirm["pairing_id"]) != pairing_id:
        raise PairingError("pairing_state_refused")
    if not verify_proof(agent_jwk, "agent", digest, confirm["agent_proof"]):
        raise PairingError("pairing_proof_refused")


def verify_session_authorization(data, identity, *, trust_set, now):
    """Agent checks for the first session record; returns the session limit."""
    record = parse_message(data, "session.authorization")
    return verify_grants(
        record["installation_grant"], record["creator_account_binding"], identity,
        trust_set=trust_set, now=now,
    )


class PairingWindow:
    """Brain's pairing window. Every transition is a compare-and-set on state."""

    def __init__(self, *, opened_at, generation, pairing_id, creator_account_id,
                 brain_noise_private, brain_nonce, installation_key, grants):
        self.deadline = opened_at + WINDOW_SECONDS
        self.state = "open"
        self.generation = generation
        self.pairing_id = pairing_id
        self.creator_account_id = creator_account_id
        self.brain_noise_private = brain_noise_private
        self.brain_noise_key = _x25519_public(brain_noise_private)
        self.brain_nonce = brain_nonce
        self.installation_key = installation_key
        self.grants = grants
        self.request = None
        self.digest = None

    def _live(self, now, state):
        if self.state in OUTCOMES:
            raise PairingError("pairing_state_refused")
        if now >= self.deadline:
            self.state = "expired"
            raise PairingError("pairing_state_refused")
        if self.state != state:
            self.state = "cancelled"
            raise PairingError("pairing_state_refused")

    def offer(self, data, now):
        self._live(now, "open")
        try:
            request = verify_request(
                data, brain_noise_key=self.brain_noise_key, brain_nonce=self.brain_nonce
            )
        except PairingError:
            self.state = "cancelled"
            raise
        installation_jwk = public_jwk(self.installation_key)
        claims = _peek(self.grants["installation_grant"])
        identity = GrantIdentity(
            claims["organization_id"], claims["installation_id"],
            claims["installation_key_id"], claims["installation_key_jkt"],
            self.creator_account_id,
        )
        offer = {
            "type": "pair.offer",
            "pairing_id": b64u(self.pairing_id),
            "generation": self.generation,
            "creator_account_id": self.creator_account_id,
            "brain_noise_key": b64u(self.brain_noise_key),
            "brain_nonce": b64u(self.brain_nonce),
            "installation_jwk": installation_jwk,
            "installation_grant": self.grants["installation_grant"],
            "creator_account_binding": self.grants["creator_account_binding"],
        }
        grants = grant_digest(offer["creator_account_binding"], offer["installation_grant"])
        self.digest = pairing_digest(transcript_of(request, offer, identity, grants))
        offer["brain_proof"] = sign_proof(self.installation_key, "brain", self.digest)
        self.request = request
        self.state = "offered"
        return offer

    def confirm(self, data, now):
        self._live(now, "offered")
        try:
            verify_confirm(
                data, pairing_id=self.pairing_id,
                agent_jwk=self.request["agent_identity_jwk"], digest=self.digest,
            )
        except PairingError:
            self.state = "cancelled"
            raise
        self.state = "proven"
        return comparison_code(self.digest)

    def decide(self, confirmed, now):
        self._live(now, "proven")
        self.state = "confirmed" if confirmed else "declined"
        return {
            "type": "pair.result", "pairing_id": b64u(self.pairing_id),
            "outcome": self.state,
        }


def _x25519_public(private):
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


# Brain-side test fixtures. Every key derives from a public label; the
# published pairing vector is vendored, not built here.

ISSUED_AT = 1_788_000_000
NOW = ISSUED_AT + 3_600
ORGANIZATION_ID = "0198a1b2-c3d4-7000-8000-0000000000a1"
INSTALLATION_ID = "0198a1b2-c3d4-7000-8000-0000000000a2"
ACCOUNT_ID = "creator-account-pairing-001"
AGENT_INSTALLATION_ID = "6f1c2c9e-3b1a-4d55-9a8e-2f0c7b1d4e10"


def _label(label):
    return _sha256(b"ofca-companion-spike-fixture/" + label.encode())


def _p256(label):
    scalar = int.from_bytes(_label(label), "big") % (_P256_ORDER - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _kid(key):
    return "bc1.ib." + b64u(unb64u(jwk_thumbprint(public_jwk(key)))[:16])


def _sign_grant(issuer, grant_type, payload):
    header = {"alg": "ES256", "kid": _kid(issuer), "typ": _GRANT_TYP[grant_type]}
    signing_input = f"{b64u(_canonical(header))}.{b64u(_canonical(payload))}"
    r, s = decode_dss_signature(issuer.sign(
        signing_input.encode("ascii"),
        ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
    ))
    s = min(s, _P256_ORDER - s)
    return f"{signing_input}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class _Fixture:
    def __init__(self):
        self.issuer = _p256("issuer")
        self.installation = _p256("installation-key")
        self.other_installation = _p256("other-installation-key")
        self.agent_identity = _p256("agent-identity-key")
        self.agent_noise = _label("agent-noise-key")
        self.brain_noise = _label("brain-noise-key")
        self.agent_nonce = _label("agent-nonce")
        self.brain_nonce = _label("brain-nonce")
        self.pairing_id = _label("pairing-id")
        self._jti = 0
        self.base_grants = self.grants()

    def grant(self, grant_type):
        self._jti += 1
        jkt = jwk_thumbprint(public_jwk(self.installation))
        payload = {
            "aud": GRANT_AUDIENCES[grant_type],
            "exp": ISSUED_AT + _GRANT_LIFETIME[grant_type],
            "grant_type": grant_type,
            "iat": ISSUED_AT,
            "installation_id": INSTALLATION_ID,
            "installation_key_id": "ik1." + b64u(unb64u(jkt)[:16]),
            "installation_key_jkt": jkt,
            "iss": "urn:bridge-clean:commercial-control-plane",
            "jti": f"0198a1b2-c3d4-7100-8000-{self._jti:012x}",
            "nbf": ISSUED_AT,
            "organization_id": ORGANIZATION_ID,
            "profile": "urn:bridge-clean:grant-profile:v1",
            "sub": f"installation:{INSTALLATION_ID}",
        }
        if grant_type == "creator_account_binding":
            payload.update({
                "approval_id": "0198a1b2-c3d4-7200-8000-0000000000a1",
                "approval_revision": 1,
                "creator_account_id": ACCOUNT_ID,
                "sub": f"installation:{INSTALLATION_ID}:creator:{ACCOUNT_ID}",
            })
        return _sign_grant(self.issuer, grant_type, payload)

    def grants(self):
        return {
            "installation_grant": self.grant("installation_grant"),
            "creator_account_binding": self.grant("creator_account_binding"),
        }

    def trust_set(self):
        jwk = public_jwk(self.issuer)
        return {
            "production_usable": False,
            "profile": "companion-spike-fixture",
            "keys": [{
                "purpose": "installation-binding",
                "jwk": {**jwk, "kid": _kid(self.issuer)},
                "thumbprint": jwk_thumbprint(jwk),
            }],
        }

    def request(self, **changes):
        message = {
            "type": "pair.request",
            "agent_installation_id": AGENT_INSTALLATION_ID,
            "agent_identity_jwk": public_jwk(self.agent_identity),
            "agent_noise_key": b64u(_x25519_public(self.agent_noise)),
            "agent_nonce": b64u(self.agent_nonce),
        }
        message.update(changes)
        return message


def case_frame(case):
    """The frame a vector case describes; pad_to_bytes appends JSON whitespace."""
    frame = case["text"].encode("utf-8")
    return frame + b" " * (case.get("pad_to_bytes", len(frame)) - len(frame))


def fixture_material(label):
    """Return the test-only material a published contract label stands for."""
    return _sha256((FIXTURE_LABEL_PREFIX + label).encode("ascii"))


def load_contract():
    """Return the vendored pairing contract once every byte matches its pin."""
    from contracts.loader import verify_snapshot_integrity

    manifest = verify_snapshot_integrity()
    listed = {entry["path"]: entry for entry in manifest["files"]}
    root = ROOT / "contracts"

    def pinned(relative):
        entry = listed.get(relative)
        if entry is None:
            raise PairingError("pairing_state_refused", f"unpinned contract file: {relative}")
        data = (root / relative).read_bytes()
        if len(data) != entry["size"] or _sha256(data).hex() != entry["sha256"]:
            raise PairingError("pairing_state_refused", f"vendored bytes differ: {relative}")
        return json.loads(data)

    family = pinned(f"{CONTRACT_EXPORT}/manifest.json")
    if family["profile"] != CONTRACT_PROFILE:
        raise PairingError("pairing_state_refused", "vendored family names another profile")
    contract = {}
    for entry in family["files"]:
        document = pinned(f"{CONTRACT_EXPORT}/{entry['path']}")
        contract[entry["path"].removesuffix(".json")] = document
    profile = pinned("companion-pairing-profile/profile.json")
    if profile["profile"] != family["profile"]:
        raise PairingError("pairing_state_refused", "vendored record names another profile")
    contract["profile"] = profile
    return contract
