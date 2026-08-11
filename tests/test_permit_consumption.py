from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.security.permit_consumption import decide_permit_consumption


VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "permit-consumption"
EXPECTED_INPUT_KEYS = {
    "durable_output_committed",
    "event",
    "installation_matches",
    "permit_state",
    "requested_job_id",
    "reserved_job_id",
}


def _cases() -> list[Path]:
    return sorted(
        path
        for path in VECTORS.glob("*.json")
        if not path.name.endswith(".expected.json") and path.name != "policy.json"
    )


@pytest.mark.contract_integrity
@pytest.mark.parametrize("case_path", _cases(), ids=lambda path: path.stem)
def test_vendored_permit_consumption_vectors_match(case_path: Path) -> None:
    case: dict[str, Any] = json.loads(case_path.read_text(encoding="utf-8"))
    expected = json.loads(
        case_path.with_name(f"{case_path.stem}.expected.json").read_text(
            encoding="utf-8"
        )
    )

    assert set(case) == EXPECTED_INPUT_KEYS
    actual = decide_permit_consumption(**case)

    assert set(actual) == {"result", "valid"}
    assert actual == expected


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            {
                "durable_output_committed": False,
                "event": "reserve",
                "installation_matches": True,
                "permit_state": "available",
                "requested_job_id": "job-a",
                "reserved_job_id": None,
            },
            {"result": "reserved", "valid": True},
        ),
        (
            {
                "durable_output_committed": False,
                "event": "reserve",
                "installation_matches": True,
                "permit_state": "reserved",
                "requested_job_id": "job-a",
                "reserved_job_id": "job-a",
            },
            {"result": "resume_existing_job", "valid": True},
        ),
        (
            {
                "durable_output_committed": False,
                "event": "terminal_failure",
                "installation_matches": True,
                "permit_state": "reserved",
                "requested_job_id": "job-a",
                "reserved_job_id": "job-a",
            },
            {"result": "released_to_available", "valid": True},
        ),
        (
            {
                "durable_output_committed": True,
                "event": "reserve",
                "installation_matches": True,
                "permit_state": "spent",
                "requested_job_id": "job-a",
                "reserved_job_id": "job-a",
            },
            {"result": "permit_spent", "valid": False},
        ),
        (
            {
                "durable_output_committed": False,
                "event": "reserve",
                "installation_matches": False,
                "permit_state": "available",
                "requested_job_id": "job-a",
                "reserved_job_id": None,
            },
            {"result": "installation_mismatch", "valid": False},
        ),
        (
            {
                "durable_output_committed": False,
                "event": "reserve",
                "installation_matches": True,
                "permit_state": "reserved",
                "requested_job_id": "job-b",
                "reserved_job_id": "job-a",
            },
            {"result": "permit_not_available", "valid": False},
        ),
    ],
    ids=[
        "reserved",
        "resume_existing_job",
        "released_to_available",
        "permit_spent",
        "installation_mismatch",
        "permit_not_available",
    ],
)
def test_each_decision_result_is_explicit(case: dict[str, Any], expected: dict[str, Any]) -> None:
    assert decide_permit_consumption(**case) == expected


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            {
                "durable_output_committed": False,
                "event": "terminal_failure",
                "installation_matches": True,
                "permit_state": "available",
                "requested_job_id": "job-a",
                "reserved_job_id": None,
            },
            {"result": "permit_not_available", "valid": False},
        ),
        (
            {
                "durable_output_committed": False,
                "event": "unrecognized",
                "installation_matches": True,
                "permit_state": "available",
                "requested_job_id": "job-a",
                "reserved_job_id": None,
            },
            {"result": "permit_not_available", "valid": False},
        ),
        (
            {
                "durable_output_committed": False,
                "event": "reserve",
                "installation_matches": True,
                "permit_state": "unrecognized",
                "requested_job_id": "job-a",
                "reserved_job_id": None,
            },
            {"result": "permit_not_available", "valid": False},
        ),
    ],
    ids=["available-terminal-failure", "unknown-event", "unknown-state"],
)
def test_uncovered_combinations_fail_closed(
    case: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert decide_permit_consumption(**case) == expected
