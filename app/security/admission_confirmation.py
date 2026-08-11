"""Confirmation records that bind one reserve request to the cost that was shown.

``credit_cost`` is a contract wire field. It is carried, displayed, and bound
verbatim; this module attaches no local meaning to its value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.security.local_sessions import (
    LocalSessionError,
    open_local_document,
    sign_local_document,
)
from app.security.runtime_policy import RuntimePolicy


CONFIRMATION_SCHEMA_VERSION = 1
CONFIRMATION_PURPOSE = "single-execution-admission-confirmation"

_DOCUMENT_FIELDS = frozenset(
    {
        "account",
        "capability",
        "confirmed_at",
        "credit_cost",
        "input_revision",
        "input_selection",
        "installation",
        "job",
        "organization",
        "parameter_digest",
        "permit",
        "principal",
        "promised_generation",
        "purpose",
        "request_idempotency_key",
        "session",
        "v",
    }
)
_DOCUMENT_TEXT_FIELDS = (
    "account",
    "capability",
    "input_selection",
    "installation",
    "job",
    "organization",
    "parameter_digest",
    "permit",
    "principal",
    "promised_generation",
    "request_idempotency_key",
    "session",
)
_DOCUMENT_INTEGER_FIELDS = ("confirmed_at", "input_revision", "v")


class AdmissionConfirmationError(LocalSessionError):
    """Raised when a confirmation document is absent, invalid, or unbound."""


@dataclass(frozen=True, slots=True)
class ConfirmationSubject:
    """The capability, input, and cost presented for one reserve request."""

    organization_id: str
    installation_id: str
    request_idempotency_key: str
    permit_id: str
    job_id: str
    promised_generation_id: str
    capability: str
    parameter_digest: str
    input_revision: int
    input_selection_digest: str
    credit_cost: Any


@dataclass(frozen=True, slots=True)
class AdmissionConfirmation:
    """One binding of user, permit, job, capability, input, cost, and time."""

    principal_id: str
    session_id: str
    creator_account_id: str
    subject: ConfirmationSubject
    confirmed_at: int
    schema_version: int = CONFIRMATION_SCHEMA_VERSION

    def durable_record(self) -> dict[str, Any]:
        """Return the field mapping recorded with the reservation."""

        return _document(self)


def displayed_credit_cost(subject: ConfirmationSubject) -> Any:
    """Return the carried cost value for display."""

    return subject.credit_cost


def issue_admission_confirmation(
    policy: RuntimePolicy,
    subject: ConfirmationSubject,
    *,
    issued_at: int | None = None,
) -> tuple[AdmissionConfirmation, str]:
    """Bind one confirmation to the authenticated session and local time."""

    identity = policy.identity
    if not identity.session_id:
        raise AdmissionConfirmationError(
            "Confirmation requires an authenticated session"
        )
    confirmation = AdmissionConfirmation(
        principal_id=identity.principal_id,
        session_id=identity.session_id,
        creator_account_id=identity.creator_account_id,
        subject=subject,
        confirmed_at=int(time.time()) if issued_at is None else issued_at,
    )
    return confirmation, sign_local_document(_document(confirmation))


def open_admission_confirmation(
    policy: RuntimePolicy,
    token: str | None,
    *,
    now: int | None = None,
) -> AdmissionConfirmation:
    """Return the confirmation carried by one token bound to this session."""

    try:
        document = open_local_document(token)
    except LocalSessionError as error:
        raise AdmissionConfirmationError("Confirmation token is invalid") from error
    if set(document) != _DOCUMENT_FIELDS:
        raise AdmissionConfirmationError("Confirmation token is invalid")
    if document.get("purpose") != CONFIRMATION_PURPOSE:
        raise AdmissionConfirmationError("Confirmation token is invalid")
    for field in _DOCUMENT_TEXT_FIELDS:
        if type(document.get(field)) is not str:
            raise AdmissionConfirmationError("Confirmation token is invalid")
    for field in _DOCUMENT_INTEGER_FIELDS:
        if type(document.get(field)) is not int:
            raise AdmissionConfirmationError("Confirmation token is invalid")
    if document.get("v") != CONFIRMATION_SCHEMA_VERSION:
        raise AdmissionConfirmationError("Confirmation token is invalid")

    confirmation = _confirmation(document)
    identity = policy.identity
    if (
        confirmation.principal_id != identity.principal_id
        or confirmation.session_id != identity.session_id
        or confirmation.creator_account_id != identity.creator_account_id
    ):
        raise AdmissionConfirmationError("Confirmation does not match the session")
    instant = int(time.time()) if now is None else now
    age = instant - confirmation.confirmed_at
    if age < 0 or age > settings.csrf_token_ttl_seconds:
        raise AdmissionConfirmationError("Confirmation has expired")
    return confirmation


def _document(confirmation: AdmissionConfirmation) -> dict[str, Any]:
    subject = confirmation.subject
    return {
        "account": confirmation.creator_account_id,
        "capability": subject.capability,
        "confirmed_at": confirmation.confirmed_at,
        "credit_cost": subject.credit_cost,
        "input_revision": subject.input_revision,
        "input_selection": subject.input_selection_digest,
        "installation": subject.installation_id,
        "job": subject.job_id,
        "organization": subject.organization_id,
        "parameter_digest": subject.parameter_digest,
        "permit": subject.permit_id,
        "principal": confirmation.principal_id,
        "promised_generation": subject.promised_generation_id,
        "purpose": CONFIRMATION_PURPOSE,
        "request_idempotency_key": subject.request_idempotency_key,
        "session": confirmation.session_id,
        "v": confirmation.schema_version,
    }


def _confirmation(document: dict[str, Any]) -> AdmissionConfirmation:
    return AdmissionConfirmation(
        principal_id=document["principal"],
        session_id=document["session"],
        creator_account_id=document["account"],
        subject=ConfirmationSubject(
            organization_id=document["organization"],
            installation_id=document["installation"],
            request_idempotency_key=document["request_idempotency_key"],
            permit_id=document["permit"],
            job_id=document["job"],
            promised_generation_id=document["promised_generation"],
            capability=document["capability"],
            parameter_digest=document["parameter_digest"],
            input_revision=document["input_revision"],
            input_selection_digest=document["input_selection"],
            credit_cost=document["credit_cost"],
        ),
        confirmed_at=document["confirmed_at"],
        schema_version=document["v"],
    )
