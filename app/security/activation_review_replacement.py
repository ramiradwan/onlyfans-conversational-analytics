"""Exact same-terms review replacement; a prepared source is not a quote or consent."""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp
from app.security.installation_continuation import PROFILE as SOURCE_PROFILE, _decode, _document, canonical_request
from app.security.activation_reconciliation import exact_request as original_activation_request
from app.security.source_replacement import SourceReplacementClient, REGISTERED, _Contract, LIMIT

NAME = "complimentary-activation-review-replacement"
PROFILE = "urn:bridge-clean:" + NAME + ":v1"
PURPOSE = "complimentary-activation-review-replace"


@lru_cache(maxsize=1)
def contract():
    return _Contract("complimentary-activation-review")


def prepare_request(request, *, successor=False):
    return {"profile": SOURCE_PROFILE, "operation_id": request[
        "successor_source_operation_id" if successor else "source_operation_id"], "scope": request["scope"]}


def replacement_request(prepare, *, continuation_id, activation_operation_id,
                        successor_source_operation_id, successor_activation_operation_id):
    value = original_activation_request(prepare, continuation_id=continuation_id,
                                        activation_operation_id=activation_operation_id)
    value.update(profile=PROFILE, successor_source_operation_id=successor_source_operation_id,
                 successor_activation_operation_id=successor_activation_operation_id)
    value = _document(canonical_request(value), limit=LIMIT)
    challenge_target(value)
    return value


def challenge_target(request):
    target = contract().validate("proof-challenge-request", {"profile": contract().proof["profile"],
        "target": {"operation": PURPOSE, "request": request}})
    if (len({request[name] for name in ("source_operation_id", "activation_operation_id",
                "successor_source_operation_id", "successor_activation_operation_id")}) != 4
            or request["source_prepare_digest"] != hashlib.sha256(canonical_request(prepare_request(request))).hexdigest()):
        raise HostedGrantUnavailable("Original review identity changed")
    return target


def validate_result(request, result, *, now):
    challenge_target(request)
    contract().validate("replace-response", result)
    successor = result["successor"]
    if (any(result[name] != request[name] for name in
            ("source_operation_id", "activation_operation_id", "successor_activation_operation_id"))
            or successor["operation_id"] != request["successor_source_operation_id"]
            or successor["continuation_id"] == request["source_continuation_id"]
            or result["successor_prepare_digest"] != hashlib.sha256(
                canonical_request(prepare_request(request, successor=True))).hexdigest()):
        raise HostedGrantUnavailable("Review replacement identity changed")
    _decode(successor["reference"], 32)
    expiry = timestamp(successor["expires_at"])
    if not now < expiry <= now + timedelta(minutes=30):
        raise HostedGrantUnavailable("Review replacement deadline is invalid")
    return result


class ActivationReviewReplacementClient(SourceReplacementClient):
    """Registered-key proof exchange; does not choose an offer or confirm it.

    The shared transport handles the closed envelope, proof checks and one send;
    this contract has its own route, domain, request and response validation.
    """
    def __init__(self, transport, key, *, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.kind, self.transport, self.key = REGISTERED, transport, key
        self.clock, self.monotonic = clock, monotonic
        self.contract = contract()
        self.purpose = PURPOSE
        self.challenge_path = f"/v1/onboarding/{NAME}-proof-challenges"
        self.route = self.contract.proof["purposes"][PURPOSE]

    def _target(self, request):
        return challenge_target(request)

    def validate_result(self, request, result, *, intended_creator_id=None):
        if intended_creator_id is not None and intended_creator_id != request["scope"]["creator_account_id"]:
            raise HostedGrantUnavailable("Review replacement creator changed")
        return validate_result(request, result, now=self.clock())
