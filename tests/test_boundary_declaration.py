"""Permanent controls for the protected-impact pull-request declaration gate."""

from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tools.check_boundary_declaration import (
    DEFAULT_MANIFEST_PATH,
    classify_architecture_impact,
    load_manifest,
    validate_gate,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "boundary_declaration"
MANIFEST = load_manifest(DEFAULT_MANIFEST_PATH)
TEMPLATE_BODY = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")


def _body(
    dispositions: str = "N/A",
    *,
    rationale: str = "N/A",
    actual: str = "N/A",
    evidence: str = "N/A",
) -> str:
    return f"""# Pull request

## Architecture impact

Architecture rationale:
{rationale}

Potentially affected invariant dispositions:
{dispositions}

Invariant / boundary actually affected:
{actual}

Safety evidence:
{evidence}
"""


def _manifest_for_fixture(fixture: dict[str, object]) -> dict[str, object]:
    manifest = copy.deepcopy(MANIFEST)
    if fixture.get("expired_temporary_exception"):
        manifest["exceptions"].append(
            {
                "source": "app/persistence/factory.py",
                "target": "app.analytics",
                "rule": "rule-persistence-factory-no-analytics",
                "reason": "Permanent protected-impact fixture for an expired exception.",
                "status": "temporary_exception",
                "introduced_by": "test fixture",
                "tracking_issue": "TEST-PROTECTED-IMPACT-EXPIRED",
                "expires": "2020-01-01",
                "removal_task": "TEST-PROTECTED-IMPACT-EXPIRED",
            }
        )
    if fixture.get("duplicate_exception_identity"):
        manifest["exceptions"].append(copy.deepcopy(manifest["exceptions"][0]))
    return manifest


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_template_is_the_exact_stable_architecture_impact_section() -> None:
    assert TEMPLATE_BODY == _body()


def test_presentation_only_frontend_change_reports_context_without_evidence() -> None:
    impact, errors = validate_gate(
        ["frontend/src/components/ConversationCard.tsx"], TEMPLATE_BODY, MANIFEST
    )
    assert impact.as_dict() == {
        "maximum_zone": "green",
        "affected_modules": ["bridge-presentation"],
        "potentially_affected_invariants": [],
        "affected_rules": [],
        "exceptions_changed": [],
        "unclassified_paths": [],
    }
    assert not errors


def test_frontend_store_maintenance_is_yellow_context_without_evidence() -> None:
    impact, errors = validate_gate(
        ["frontend/src/store/session.ts"], TEMPLATE_BODY, MANIFEST
    )
    assert impact.maximum_zone == "yellow"
    assert impact.affected_modules == ("bridge-orchestration",)
    assert not impact.potentially_affected_invariants
    assert not errors


def test_red_extension_asset_alone_is_context_not_protected_impact() -> None:
    impact, errors = validate_gate(["extension/icons/icon.svg"], TEMPLATE_BODY, MANIFEST)
    assert impact.maximum_zone == "red"
    assert impact.affected_modules == ("agent-runtime",)
    assert not impact.potentially_affected_invariants
    assert not errors


def test_dot_prefixed_ci_path_keeps_its_manifest_classification() -> None:
    impact, errors = validate_gate(["./.github/workflows/ci.yml"], _body(
        "release-integrity:\nnot affected\nrationale: This test only proves path normalization."
    ), MANIFEST)
    assert impact.maximum_zone == "red"
    assert impact.affected_modules == ("ci-workflows",)
    assert impact.potentially_affected_invariants == ("release-integrity",)
    assert not errors


def test_graph_identity_mapping_requires_an_explicit_disposition() -> None:
    impact, errors = validate_gate(["app/analytics/identity.py"], TEMPLATE_BODY, MANIFEST)
    assert impact.maximum_zone == "orange"
    assert impact.potentially_affected_invariants == ("graph-identity",)
    assert any("N/A is not a valid disposition" in error for error in errors)


def test_not_affected_graph_identity_with_rationale_does_not_need_evidence() -> None:
    body = _body(
        "graph-identity:\nnot affected\nrationale: Comment-only clarification; graph identifiers and executable code are unchanged."
    )
    _, errors = validate_gate(["app/analytics/identity.py"], body, MANIFEST)
    assert not errors


def test_affected_graph_identity_requires_rationale_declaration_and_evidence() -> None:
    body = _body(
        "graph-identity:\naffected",
        rationale="The identifier derivation changes while retaining canonical-ID determinism.",
        actual="graph-identity",
        evidence="python -m pytest tests/stateful/test_analytics_determinism.py",
    )
    _, errors = validate_gate(["app/analytics/identity.py"], body, MANIFEST)
    assert not errors


def test_affected_invariant_identifier_spoof_is_not_a_boundary_declaration() -> None:
    body = _body(
        "graph-identity:\naffected",
        rationale="The identifier derivation changes while retaining canonical-ID determinism.",
        actual="foo_graph-identity_bar",
        evidence="python -m pytest tests/stateful/test_analytics_determinism.py",
    )
    _, errors = validate_gate(["app/analytics/identity.py"], body, MANIFEST)
    assert any("must name affected invariant 'graph-identity'" in error for error in errors)


def test_enforced_rule_definition_change_requires_evidence() -> None:
    previous = copy.deepcopy(MANIFEST)
    current = copy.deepcopy(MANIFEST)
    rule = next(
        rule
        for rule in current["rules"]
        if rule["id"] == "rule-canonical-persistence-no-upward"
    )
    rule["description"] += " Changed boundary definition."

    impact, errors = validate_gate(
        ["docs/architecture-boundaries.json"], TEMPLATE_BODY, current, base_manifest=previous
    )
    assert impact.affected_rules == ("rule-canonical-persistence-no-upward",)
    assert any("Safety evidence" in error for error in errors)


def test_enforced_rule_identifier_spoof_is_not_a_boundary_declaration() -> None:
    previous = copy.deepcopy(MANIFEST)
    current = copy.deepcopy(MANIFEST)
    rule_id = "rule-canonical-persistence-no-upward"
    rule = next(rule for rule in current["rules"] if rule["id"] == rule_id)
    rule["description"] += " Changed boundary definition."
    body = _body(
        rationale="The dependency boundary definition changes with an explicit review.",
        actual=f"foo_{rule_id}_bar",
        evidence="python -m pytest tests/test_architecture_contracts.py",
    )

    _, errors = validate_gate(
        ["docs/architecture-boundaries.json"], body, current, base_manifest=previous
    )
    assert any(f"must name affected rule '{rule_id}'" in error for error in errors)


def test_exception_ledger_change_requires_evidence() -> None:
    previous = copy.deepcopy(MANIFEST)
    current = copy.deepcopy(MANIFEST)
    current["exceptions"][0]["reason"] += " Updated rationale."

    impact, errors = validate_gate(
        ["docs/architecture-boundaries.json"], TEMPLATE_BODY, current, base_manifest=previous
    )
    assert impact.exceptions_changed == (
        "rule-projection-coordination-boundary: app/persistence/projection_activation.py -> app.analytics",
    )
    assert any("Safety evidence" in error for error in errors)


def test_exception_addition_and_removal_each_require_evidence() -> None:
    previous = copy.deepcopy(MANIFEST)
    added = copy.deepcopy(MANIFEST)
    added["exceptions"].append(
        {
            "source": "app/persistence/factory.py",
            "target": "app.analytics.canonical_source",
            "rule": "rule-persistence-factory-no-analytics",
            "reason": "Permanent gate fixture for exception-ledger addition.",
            "status": "temporary_exception",
            "introduced_by": "test fixture",
            "tracking_issue": "TEST-EXCEPTION-ADDITION",
            "expires": "2027-12-31",
            "removal_task": "TEST-EXCEPTION-ADDITION",
        }
    )
    removed = copy.deepcopy(MANIFEST)
    removed["exceptions"] = []

    for current in (added, removed):
        impact, errors = validate_gate(
            ["docs/architecture-boundaries.json"], TEMPLATE_BODY, current, base_manifest=previous
        )
        assert impact.exceptions_changed
        assert any("Safety evidence" in error for error in errors)


@pytest.mark.parametrize(
    ("opener", "inner_fence", "closer"),
    [
        ("````markdown", "```", "````"),
        ("~~~~markdown", "~~~", "~~~~"),
        ("````markdown", "~~~~", "````"),
        ("~~~~markdown", "```", "~~~~"),
    ],
)
def test_declaration_inside_long_or_mismatched_fence_is_invisible(
    opener: str, inner_fence: str, closer: str
) -> None:
    body = f"{opener}\n{inner_fence}\n{_body()}\n{inner_fence}\n{closer}\n"
    _, errors = validate_gate(["frontend/src/components/ConversationCard.tsx"], body, MANIFEST)
    assert any("missing the exact '## Architecture impact' section" in error for error in errors)


def test_whole_architecture_impact_section_indented_as_code_is_invisible() -> None:
    body = "\n".join(
        f"    {line}" if line else line for line in _body().splitlines()
    )
    _, errors = validate_gate(["frontend/src/components/ConversationCard.tsx"], body, MANIFEST)
    assert any("missing the exact '## Architecture impact' section" in error for error in errors)


def test_real_declaration_after_an_indented_code_example_is_recognized() -> None:
    indented_example = "\n".join(
        f"    {line}" if line else line for line in _body().splitlines()
    )
    body = f"{indented_example}\n\n{_body()}"
    _, errors = validate_gate(["frontend/src/components/ConversationCard.tsx"], body, MANIFEST)
    assert not errors


def test_indented_disposition_continuation_under_a_real_field_is_preserved() -> None:
    body = _body(
        "    graph-identity:\n    not affected\n    rationale: Comment-only clarification; graph identifiers and executable code are unchanged."
    )
    _, errors = validate_gate(["app/analytics/identity.py"], body, MANIFEST)
    assert not errors


@pytest.mark.parametrize(
    "fixture_name",
    [
        "protected_impact_na_evidence",
        "matched_invariant_na_disposition",
        "matched_invariant_not_affected_without_rationale",
        "unclassified_production_path",
        "expired_temporary_exception",
        "duplicate_disposition",
        "unknown_disposition",
        "template_boilerplate",
        "duplicate_section",
        "duplicate_field",
        "duplicate_exception_identity",
    ],
)
def test_permanent_invalid_declaration_fixtures_are_rejected(fixture_name: str) -> None:
    fixture = _fixture(fixture_name)
    manifest = _manifest_for_fixture(fixture)
    _, errors = validate_gate(
        fixture["changed_files"],
        fixture["body"],
        manifest,
        reference_date=dt.date(2026, 9, 10),
    )
    assert any(fixture["expected_diagnostic"].lower() in error.lower() for error in errors), errors


def test_unclassified_path_is_reported_deterministically_even_with_other_paths() -> None:
    impact = classify_architecture_impact(
        ["frontend/src/components/ConversationCard.tsx", "app/new_runtime/worker.py"], MANIFEST
    )
    assert impact.maximum_zone == "green"
    assert impact.unclassified_paths == ("app/new_runtime/worker.py",)


def test_cli_accepts_explicit_changed_file_and_body_file_without_network(tmp_path: Path) -> None:
    body_file = tmp_path / "pr-body.md"
    body_file.write_text(TEMPLATE_BODY, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "tools/check_boundary_declaration.py",
            "--changed-file",
            "frontend/src/components/ConversationCard.tsx",
            "--pr-body-file",
            str(body_file),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.split("Architecture impact declaration passed.")[0])
    assert report["architecture_impact"]["maximum_zone"] == "green"


def test_required_build_and_test_job_runs_the_pull_request_gate_with_local_inputs() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["build-and-test"]
    checkout = next(step for step in job["steps"] if step.get("name") == "Checkout")
    assert checkout["with"]["fetch-depth"] == 0

    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Check protected architecture impact declaration"
    )
    assert step["if"] == "github.event_name == 'pull_request'"
    command = step["run"]
    assert "tools/check_boundary_declaration.py" in command
    assert '--event-path "$GITHUB_EVENT_PATH"' in command
    assert "github.event.pull_request.base.sha" in command
    assert "github.event.pull_request.head.sha" in command
