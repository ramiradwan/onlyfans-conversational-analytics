"""Bounded compact ES256 primitives; callers own keys and payload policy."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Callable, TypeVar, Generic

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

MAX_SAFE_INTEGER = 2**53 - 1
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
T = TypeVar("T")


class SignedObjectRejected(ValueError):
    def __init__(self):
        super().__init__("Signed object verification failed")


def require(condition):
    if not condition:
        raise SignedObjectRejected()


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(value, maximum):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None
            and len(value) <= (maximum * 4 + 2) // 3 and len(value) % 4 != 1)
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise SignedObjectRejected() from None
    require(len(result) <= maximum and encode(result) == value)
    return result


def parse(value, *, maximum, canonicalizer=None):
    require(type(value) is bytes and 0 < len(value) <= maximum)

    def pairs(items):
        result = {}
        for name, child in items:
            require(name not in result)
            result[name] = child
        return result

    def walk(child, depth=0):
        require(depth <= 16)
        if type(child) is str:
            require(not any(0xD800 <= ord(c) <= 0xDFFF for c in child))
        elif type(child) is int:
            require(abs(child) <= MAX_SAFE_INTEGER)
        elif type(child) is float:
            require(math.isfinite(child) and (child != int(child) or abs(child) <= MAX_SAFE_INTEGER))
        elif type(child) is dict:
            for name, item in child.items():
                walk(name, depth + 1)
                walk(item, depth + 1)
        elif type(child) is list:
            for item in child:
                walk(item, depth + 1)

    try:
        text = value.decode("utf-8", errors="strict")
        require(not text.startswith("\ufeff"))
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: require(False))
        walk(result)
        if canonicalizer is not None:
            require(canonicalizer(result) == value)
        return result
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise SignedObjectRejected() from None


def public_key(value):
    require(type(value) is dict and set(value) == {"kty", "crv", "x", "y"})
    require(value["kty"] == "EC" and value["crv"] == "P-256")
    x, y = decode(value["x"], 32), decode(value["y"], 32)
    require(len(x) == len(y) == 32)
    try:
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
    except ValueError:
        raise SignedObjectRejected() from None


@dataclass(frozen=True)
class VerifiedObject(Generic[T]):
    payload: T
    payload_sha256: str
    kid: str


def verify_compact(jws, *, typ, keys, maximum, envelope_limit,
                   header_canonicalizer, payload_validator: Callable[[bytes], T]) -> VerifiedObject[T]:
    require(type(jws) is str and len(jws) <= envelope_limit)
    segments = jws.split(".")
    require(len(segments) == 3)
    header = parse(decode(segments[0], 512), maximum=512, canonicalizer=header_canonicalizer)
    raw = decode(segments[1], maximum)
    sig = decode(segments[2], 64)
    require(type(header) is dict and set(header) == {"alg", "kid", "typ"})
    require(header["alg"] == "ES256" and header["typ"] == typ and len(sig) == 64
            and type(header["kid"]) is str and IDENTIFIER.fullmatch(header["kid"]) is not None)
    signer = next((key for key in keys if key["kid"] == header["kid"]), None)
    require(signer is not None)
    try:
        der = utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        public_key(signer["jwk"]).verify(der, (segments[0] + "." + segments[1]).encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except Exception:
        raise SignedObjectRejected() from None
    return VerifiedObject(payload_validator(raw), hashlib.sha256(raw).hexdigest(), header["kid"])
