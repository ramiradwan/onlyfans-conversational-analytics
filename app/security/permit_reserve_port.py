"""The reserve entry point for the single-execution admission profile.

Reserving requires a confirmation token. The confirmed cost and the cost
carried by the signed permit at reserve time are compared for exact equality;
a difference is refused rather than repriced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.security.admission_confirmation import (
    AdmissionConfirmation,
    AdmissionConfirmationError,
    open_admission_confirmation,
)
from app.security.permit_consumption import decide_permit_consumption
from app.security.runtime_policy import RuntimePolicy


@dataclass(frozen=True, slots=True)
class ReserveRequest:
    """Reserve-time facts compared with the confirmation before admission.

    ``permit_credit_cost`` is the value carried by the signed permit at reserve
    time. It is compared and never interpreted.
    """

    permit_id: str
    job_id: str
    capability: str
    parameter_digest: str
    input_revision: int
    input_selection_digest: str
    request_idempotency_key: str
    permit_credit_cost: Any
    installation_matches: Any
    permit_state: Any
    reserved_job_id: Any
    durable_output_committed: Any


@dataclass(frozen=True, slots=True)
class ReserveOutcome:
    """A named reserve outcome and the confirmation it was evaluated against."""

    reserved: bool
    result: str
    confirmation: AdmissionConfirmation | None = None


def reserve_admission(
    policy: RuntimePolicy,
    confirmation_token: str,
    *,
    request: ReserveRequest,
    now: int | None = None,
) -> ReserveOutcome:
    """Reserve one admission for a request whose confirmation still applies."""

    try:
        confirmation = open_admission_confirmation(policy, confirmation_token, now=now)
    except AdmissionConfirmationError:
        return ReserveOutcome(False, "confirmation_invalid")

    subject = confirmation.subject
    if subject.capability != request.capability:
        return ReserveOutcome(False, "confirmation_capability_mismatch", confirmation)
    if not _same_carried_value(subject.credit_cost, request.permit_credit_cost):
        return ReserveOutcome(False, "confirmation_cost_mismatch", confirmation)
    if (
        subject.permit_id != request.permit_id
        or subject.job_id != request.job_id
        or subject.parameter_digest != request.parameter_digest
        or subject.input_revision != request.input_revision
        or subject.input_selection_digest != request.input_selection_digest
        or subject.request_idempotency_key != request.request_idempotency_key
    ):
        return ReserveOutcome(False, "confirmation_binding_mismatch", confirmation)

    decision = decide_permit_consumption(
        durable_output_committed=request.durable_output_committed,
        event="reserve",
        installation_matches=request.installation_matches,
        permit_state=request.permit_state,
        requested_job_id=subject.job_id,
        reserved_job_id=request.reserved_job_id,
    )
    return ReserveOutcome(decision["valid"], decision["result"], confirmation)


def _same_carried_value(confirmed: Any, presented: Any) -> bool:
    """Compare two carried values for exact equality of type and value."""

    return type(confirmed) is type(presented) and confirmed == presented
