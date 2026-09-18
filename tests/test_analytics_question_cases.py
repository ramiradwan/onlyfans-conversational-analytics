"""Check fixed question cases without running a production query implementation."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from tests.state_models.analytics_question_cases import load_cases, validate_case

CASES = load_cases()
BY_ID = {case["id"]: case for case in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_question_case_has_valid_identity_time_and_evidence(case) -> None:
    validate_case(case)


def test_case_names_are_unique_and_cover_both_questions() -> None:
    assert len(BY_ID) == len(CASES) == 36
    assert {case["question"] for case in CASES} == {
        "no_later_creator_reply.v1", "pricing_discussions.v1",
    }


def test_required_edge_cases_have_literal_expectations() -> None:
    assert BY_ID["reply-after-selected-window"]["expected"]["matches"] == []
    assert BY_ID["reply-at-cutoff"]["expected"]["matches"] == []
    assert BY_ID["equal-time-inferred-order"]["expected"]["undetermined"] == ["chat-a"]
    assert BY_ID["pricing-edited-source"]["expected"]["undetermined"] == ["chat-a"]
    assert BY_ID["pricing-multiple-matches"]["expected"]["evidence"] == {"chat-a": ["m1", "m2"]}
    assert BY_ID["retention-boundary"]["expected"]["matches"] == []
    assert BY_ID["selection-boundaries"]["expected"]["matches"] == ["chat-start"]
    assert BY_ID["account-isolation"]["expected"]["matches"] == ["chat-a"]


def test_fixture_loader_does_not_import_application_code() -> None:
    path = Path(__file__).parent / "state_models" / "analytics_question_cases.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = {"__future__", "datetime", "json", "pathlib", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "__import__"}


@pytest.mark.parametrize("change,error", [
    ("duplicate_message", "duplicate_canonical_message"),
    ("duplicate_result", "duplicate_conversation_result"),
    ("foreign_evidence", "evidence_account_invalid"),
    ("deleted_evidence", "evidence_ineligible"),
    ("expired_evidence", "evidence_ineligible"),
    ("empty_window", "time_window_invalid"),
    ("naive_time", "timestamp_requires_timezone"),
    ("contradiction", "contradictory_result"),
    ("missing_evidence", "evidence_required"),
])
def test_invalid_expected_cases_are_rejected(change: str, error: str) -> None:
    case = deepcopy(BY_ID["latest-inbound"])
    if change == "duplicate_message":
        case["messages"].append(deepcopy(case["messages"][0]))
    elif change == "duplicate_result":
        case["expected"]["matches"].append("chat-a")
    elif change == "foreign_evidence":
        case["expected"]["evidence"]["chat-a"] = ["foreign-message"]
    elif change == "deleted_evidence":
        case["messages"][0]["deleted"] = True
    elif change == "expired_evidence":
        case["messages"][0]["at"] = "2026-06-20T12:00:00Z"
    elif change == "empty_window":
        case["end"] = case["start"]
    elif change == "naive_time":
        case["cutoff"] = "2026-09-10T12:00:00"
    elif change == "contradiction":
        case["expected"]["undetermined"] = ["chat-a"]
    elif change == "missing_evidence":
        case["expected"]["evidence"] = {}
    with pytest.raises(ValueError, match=error):
        validate_case(case)


def test_pricing_evidence_cannot_use_an_edited_source_version() -> None:
    case = deepcopy(BY_ID["pricing-multiple-matches"])
    case["messages"][0]["version"] = 2
    with pytest.raises(ValueError, match="evidence_version_invalid"):
        validate_case(case)
