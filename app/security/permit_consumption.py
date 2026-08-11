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

    if permit_state == "spent":
        return {"result": "permit_spent", "valid": False}

    if event == "terminal_failure":
        if permit_state == "reserved" and durable_output_committed is False:
            return {"result": "released_to_available", "valid": True}
        return {"result": "permit_not_available", "valid": False}

    if event == "reserve":
        if permit_state == "available":
            return {"result": "reserved", "valid": True}
        if (
            permit_state == "reserved"
            and reserved_job_id == requested_job_id
        ):
            return {"result": "resume_existing_job", "valid": True}

    return {"result": "permit_not_available", "valid": False}


evaluate_permit_consumption = decide_permit_consumption
