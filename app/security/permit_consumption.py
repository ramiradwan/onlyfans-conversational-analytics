"""Pure permit-consumption decision rules."""

from __future__ import annotations

from typing import Any


def decide_permit_consumption(
    durable_output_committed: Any,
    event: Any,
    installation_matches: Any,
    permit_state: Any,
    requested_job_id: Any,
    reserved_job_id: Any,
) -> dict[str, Any]:
    """Return the permit-consumption result for one policy input."""
    if installation_matches is not True:
        return {"result": "installation_mismatch", "valid": False}

    if type(permit_state) is str and permit_state == "spent":
        return {"result": "permit_spent", "valid": False}

    if (
        durable_output_committed is False
        and type(event) is str
        and event == "reserve"
        and type(permit_state) is str
        and permit_state == "available"
        and type(requested_job_id) is str
        and reserved_job_id is None
    ):
        return {"result": "reserved", "valid": True}

    if (
        durable_output_committed is False
        and type(event) is str
        and event == "reserve"
        and type(permit_state) is str
        and permit_state == "reserved"
        and type(requested_job_id) is str
        and type(reserved_job_id) is str
        and reserved_job_id == requested_job_id
    ):
        return {"result": "resume_existing_job", "valid": True}

    if (
        durable_output_committed is False
        and type(event) is str
        and event == "terminal_failure"
        and type(permit_state) is str
        and permit_state == "reserved"
        and type(requested_job_id) is str
        and type(reserved_job_id) is str
        and reserved_job_id == requested_job_id
    ):
        return {"result": "released_to_available", "valid": True}

    return {"result": "permit_not_available", "valid": False}


evaluate_permit_consumption = decide_permit_consumption
