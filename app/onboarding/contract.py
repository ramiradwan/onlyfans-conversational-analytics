"""Closed Product-owned desktop messages, independent of hosted authority."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError

from app.core.resource_paths import resource_path

PROFILE = "desktop-onboarding.v1"
SNAPSHOT_PROFILE = "desktop-onboarding.v2"
ACTIONS = frozenset({"browser.access", "account.connect", "account.confirm", "setup.finalize", "browser.pair", "activation.review", "activation.confirm", "auth.select", "auth.cancel", "auth.switch"})
REASONS = frozenset({"none", "operation_unconfirmed", "authority_unavailable", "browser_unavailable",
                     "account_mismatch", "operation_expired", "operation_refused", "activation_unavailable"})


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@lru_cache(maxsize=1)
def validator():
    document = json.loads(resource_path("shared/desktop-onboarding/schema-v2.json").read_text("utf-8"))
    return Draft202012Validator(document)


def validate(value: dict, *, kind: str) -> dict:
    try:
        if not isinstance(value, dict) or value.get("kind") != kind or len(canonical(value)) > 16_384:
            raise ValueError()
        validator().validate(value)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ValueError("Invalid desktop setup message") from None
    return value
