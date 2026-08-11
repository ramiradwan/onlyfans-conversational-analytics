from __future__ import annotations

import json
from itertools import product
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
REPRESENTATIVE_JOB_ID_PAIRS = (
    ("job-fixture-001", None),
    ("job-fixture-001", "job-fixture-001"),
    ("job-fixture-002", "job-fixture-001"),
    (None, None),
)
REPRESENTATIVE_INPUT_VALUES = {
    "durable_output_committed": (False, True, None),
    "event": ("reserve", "terminal_failure", "unrecognized"),
    "installation_matches": (True, False, None),
    "permit_state": ("available", "reserved", "spent", "unrecognized"),
}


class _UnsupportedValue:
    def __eq__(self, other: object) -> bool:
        raise AssertionError("unsupported value equality was evaluated")


def _cases() -> list[Path]:
    return sorted(
        path
        for path in VECTORS.glob("*.json")
        if not path.name.endswith(".expected.json") and path.name != "policy.json"
    )


def _input_tuple(case: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(case[key] for key in sorted(EXPECTED_INPUT_KEYS))


def _representative_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for values in product(*REPRESENTATIVE_INPUT_VALUES.values()):
        for requested_job_id, reserved_job_id in REPRESENTATIVE_JOB_ID_PAIRS:
            cases.append(
                {
                    **dict(zip(REPRESENTATIVE_INPUT_VALUES, values, strict=True)),
                    "requested_job_id": requested_job_id,
                    "reserved_job_id": reserved_job_id,
                }
            )
    return cases


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


def test_unsupported_parameter_shapes_fail_closed() -> None:
    unsupported_value = _UnsupportedValue()

    assert decide_permit_consumption(
        unsupported_value,
        unsupported_value,
        True,
        unsupported_value,
        unsupported_value,
        unsupported_value,
    ) == {"result": "permit_not_available", "valid": False}


@pytest.mark.contract_integrity
def test_uncovered_representative_combinations_fail_closed() -> None:
    vector_cases = [
        json.loads(case_path.read_text(encoding="utf-8")) for case_path in _cases()
    ]
    vector_tuples = {_input_tuple(case) for case in vector_cases}
    representative_cases = _representative_cases()
    uncovered_cases = [
        case for case in representative_cases if _input_tuple(case) not in vector_tuples
    ]
    unexpected_permissive = [
        case
        for case in uncovered_cases
        if decide_permit_consumption(**case)["valid"] is True
    ]

    print(
        "VECTOR_TUPLES="
        f"{len(vector_tuples)} "
        "REPRESENTATIVE_COMBINATIONS="
        f"{len(representative_cases)} "
        f"COVERED={len(representative_cases) - len(uncovered_cases)} "
        f"UNCOVERED={len(uncovered_cases)} "
        "UNEXPECTED_PERMISSIVE_UNCOVERED="
        f"{len(unexpected_permissive)}"
    )

    assert len(vector_tuples) == 6
    assert len(representative_cases) == 432
    assert len(uncovered_cases) == 426
    assert unexpected_permissive == [], [
        (
            case["event"],
            case["requested_job_id"],
            case["reserved_job_id"],
        )
        for case in unexpected_permissive
    ]
