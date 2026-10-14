"""Independent, bounded ES256 verification of public signing parameters.

No transport or account authority lives here. Errors contain no rejected bytes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

ENGINE = "onlyfans-sha1-checksum/v1"
PARAMETER_SCHEMA = "local-packaged-signing-rule/v1"
OBJECT_LIMIT = 16 * 1024
CATALOG_LIMIT = 32 * 1024
KEYSET_LIMIT = 16 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
PRODUCTION_TRUST_SHA256 = "9a0965a3684afae9b4745d9c36845abb2f0a168ce3b1da3256567720542873c9"
REVISION = re.compile(r"[\x21-\x7e]{1,128}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PublicationRejected(ValueError):
    def __init__(self):
        super().__init__("Signing parameter publication is not eligible")


def require(condition):
    if not condition:
        raise PublicationRejected()


def closed(value, fields):
    require(type(value) is dict and set(value) == set(fields))
    return value


def integer(value, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value == int(value) and minimum <= value <= MAX_SAFE_INTEGER


def canonical(value):
    def normalized(child):
        if type(child) is float and math.isfinite(child) and child == int(child):
            return int(child)
        if type(child) is dict:
            return {key: normalized(item) for key, item in child.items()}
        if type(child) is list:
            return [normalized(item) for item in child]
        return child
    return json.dumps(normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(value, maximum):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None
            and len(value) <= (maximum * 4 + 2) // 3 and len(value) % 4 != 1)
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise PublicationRejected() from None
    require(len(result) <= maximum and encode(result) == value)
    return result


def parse(value, *, maximum, signed=False):
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
        if signed:
            require(canonical(result) == value)
        return result
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise PublicationRejected() from None


def public_key(value):
    closed(value, ("kty", "crv", "x", "y"))
    require(value["kty"] == "EC" and value["crv"] == "P-256")
    x, y = decode(value["x"], 32), decode(value["y"], 32)
    require(len(x) == len(y) == 32)
    try:
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
    except ValueError:
        raise PublicationRejected() from None


def validate_trust(value, *, production=False):
    closed(value, ("schema", "roots"))
    require(value["schema"] == "signing-publication-trust/v1"
            and type(value["roots"]) is list and 1 <= len(value["roots"]) <= 4)
    seen = set()
    for root in value["roots"]:
        closed(root, ("kid", "jwk"))
        require(type(root["kid"]) is str and IDENTIFIER.fullmatch(root["kid"]) is not None and root["kid"] not in seen)
        seen.add(root["kid"])
        public_key(root["jwk"])
    # Pin complete production material, so renaming a synthetic key cannot pass.
    require(not production or sha256(canonical(value)) == PRODUCTION_TRUST_SHA256)
    return value


@dataclass(frozen=True)
class VerifiedPublication:
    payload: dict
    payload_sha256: str
    kid: str
    jws: str


def signature(jws, typ, keys, maximum):
    require(type(jws) is str and len(jws) <= (maximum * 4 + 2) // 3 + 1024)
    segments = jws.split(".")
    require(len(segments) == 3)
    header = parse(decode(segments[0], 512), maximum=512, signed=True)
    raw = decode(segments[1], maximum)
    sig = decode(segments[2], 64)
    closed(header, ("alg", "kid", "typ"))
    require(header["alg"] == "ES256" and header["typ"] == typ and len(sig) == 64
            and type(header["kid"]) is str and IDENTIFIER.fullmatch(header["kid"]) is not None)
    signer = next((key for key in keys if key["kid"] == header["kid"]), None)
    require(signer is not None)
    try:
        der = utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        public_key(signer["jwk"]).verify(der, (segments[0] + "." + segments[1]).encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except Exception:
        raise PublicationRejected() from None
    return VerifiedPublication(parse(raw, maximum=maximum, signed=True), sha256(raw), header["kid"], jws)


def lifetime(value, now, maximum, allow_expired=False):
    require(integer(now) and integer(value["issued_at"]) and integer(value["expires_at"], 1)
            and 0 < value["expires_at"] - value["issued_at"] <= maximum
            and value["issued_at"] <= now + 300
            and (allow_expired or now < value["expires_at"]))


def verify_keyset(jws, trust, *, now, allow_expired=False):
    validate_trust(trust)
    verified = signature(jws, "signing-publisher-keyset+jws", trust["roots"], KEYSET_LIMIT)
    value = closed(verified.payload, ("schema", "sequence", "issued_at", "expires_at", "keys"))
    require(value["schema"] == "signing-publisher-keyset/v1" and integer(value["sequence"], 1)
            and type(value["keys"]) is list and 1 <= len(value["keys"]) <= 8)
    lifetime(value, now, 366 * 86400, allow_expired)
    seen = set()
    for key in value["keys"]:
        closed(key, ("kid", "jwk", "not_before", "not_after"))
        require(type(key["kid"]) is str and IDENTIFIER.fullmatch(key["kid"]) is not None and key["kid"] not in seen
                and integer(key["not_before"]) and integer(key["not_after"], 1)
                and value["issued_at"] <= key["not_before"] < key["not_after"] <= value["expires_at"])
        seen.add(key["kid"])
        public_key(key["jwk"])
        require(not any(root["kid"] == key["kid"] or root["jwk"] == key["jwk"] for root in trust["roots"]))
    return verified


def verify_catalog(jws, keyset, *, now, allow_expired=False):
    require(type(keyset) is VerifiedPublication)
    verified = signature(jws, "signing-parameter-catalog+jws", keyset.payload["keys"], CATALOG_LIMIT)
    value = closed(verified.payload, ("schema", "sequence", "issued_at", "expires_at", "platform", "engine", "parameter_schema", "rules"))
    require(value["schema"] == "signing-parameter-catalog/v1" and integer(value["sequence"], 1)
            and value["platform"] == "onlyfans"
            and all(type(value[field]) is str and re.fullmatch(r"[\x21-\x7e]{1,64}", value[field]) for field in ("engine", "parameter_schema"))
            and type(value["rules"]) is list and len(value["rules"]) <= 128)
    lifetime(value, now, 7 * 86400, allow_expired)
    signer = next(key for key in keyset.payload["keys"] if key["kid"] == verified.kid)
    require(signer["not_before"] <= value["issued_at"] and value["expires_at"] <= signer["not_after"]
            and (allow_expired or now < keyset.payload["expires_at"]))
    previous = None
    for entry in value["rules"]:
        closed(entry, ("source_revision", "sha256", "size"))
        revision = entry["source_revision"]
        require(type(revision) is str and REVISION.fullmatch(revision) is not None
                and (previous is None or previous < revision) and type(entry["sha256"]) is str
                and DIGEST.fullmatch(entry["sha256"]) is not None and integer(entry["size"], 1) and entry["size"] <= OBJECT_LIMIT)
        previous = revision
    return verified


def verify_object(raw, entry):
    require(type(raw) is bytes and len(raw) == entry["size"] and sha256(raw) == entry["sha256"])
    rule = closed(parse(raw, maximum=OBJECT_LIMIT), ("schema", "source_revision", "static_param", "format"))
    require(rule["schema"] == PARAMETER_SCHEMA and rule["source_revision"] == entry["source_revision"]
            and type(rule["static_param"]) is str and 1 <= len(rule["static_param"].encode("utf-16-le")) // 2 <= 4096
            and re.search(r"[\x00-\x1f\x7f]", rule["static_param"]) is None)
    fmt = closed(rule["format"], ("prefix", "suffix", "checksum_indexes", "checksum_constant"))
    require(all(type(fmt[field]) is str and re.fullmatch(r"[\x21-\x39\x3b-\x7e]{1,128}", fmt[field]) for field in ("prefix", "suffix")))
    indexes = fmt["checksum_indexes"]
    constant = fmt["checksum_constant"]
    require(type(indexes) is list and 1 <= len(indexes) <= 256 and all(integer(i) and i < 40 for i in indexes)
            and integer(constant, -MAX_SAFE_INTEGER) and constant <= MAX_SAFE_INTEGER - 102 * len(indexes))
    return rule


def checkpoint(verified):
    return {"jws": verified.jws, "sequence": verified.payload["sequence"], "payload_sha256": verified.payload_sha256}


def check_checkpoint(saved, verified):
    closed(saved, ("jws", "sequence", "payload_sha256"))
    require(saved == checkpoint(verified))


def monotonic(previous, verified):
    if previous is not None:
        require(verified.payload["sequence"] >= previous["sequence"]
                and (verified.payload["sequence"] != previous["sequence"] or verified.payload_sha256 == previous["payload_sha256"]))
