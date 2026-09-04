"""Behavioural falsifiers for the Windows package workflow's release coordinates.

Each check is asserted against the workflow as written and then against a copy
with one thing changed, so a check that could only ever pass is visible as one
that no mutation turns red.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-package.yml"
GATE_SCRIPT = "tools/legal-release-bindings/verify.mjs"
PACKAGE_BUILD_COMMAND = ".\\packaging\\build-windows.ps1"

# The two Legal coordinates are independent: the document is fetched at one and
# carries the other. Requiring them to be equal would refuse a valid release.
SOURCE_REVISION_INPUT = "legal_repository_revision"
FETCH_REVISION_INPUT = "legal_bindings_repository_revision"

REQUIRED_INPUTS = (
    "product_revision",
    SOURCE_REVISION_INPUT,
    FETCH_REVISION_INPUT,
    "legal_bindings_path",
    "legal_bindings_digest",
    "signing_rule_release_tag",
    "signing_rule_release_asset_id",
    "signing_rule_digest",
    "signing_rule_source_revision",
)

# Each coordinate reaches the gate under its own name, so no two of them can
# resolve to one value.
COORDINATE_ENVIRONMENT = {
    "PRODUCT_REVISION": "${{ inputs.product_revision }}",
    "LEGAL_REPOSITORY_REVISION": "${{ inputs.legal_repository_revision }}",
    "LEGAL_BINDINGS_REPOSITORY_REVISION": (
        "${{ inputs.legal_bindings_repository_revision }}"
    ),
    "LEGAL_BINDINGS_PATH": "${{ inputs.legal_bindings_path }}",
    "LEGAL_BINDINGS_DIGEST": "${{ inputs.legal_bindings_digest }}",
}

# Credentials that do not exist yet. Absence must stop the release at the gate,
# which is what the gate's own suite exercises; this file only fixes the names.
CREDENTIAL_ENVIRONMENT = {
    "LEGAL_BINDINGS_REPOSITORY": "${{ secrets.LEGAL_BINDINGS_REPOSITORY }}",
    "LEGAL_BINDINGS_APP_ID": "${{ secrets.LEGAL_BINDINGS_APP_ID }}",
    "LEGAL_BINDINGS_APP_PRIVATE_KEY_B64": (
        "${{ secrets.LEGAL_BINDINGS_APP_PRIVATE_KEY_B64 }}"
    ),
    "LEGAL_BINDINGS_INSTALLATION_ID": "${{ secrets.LEGAL_BINDINGS_INSTALLATION_ID }}",
}


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads a bare ``on`` key as the boolean it also spells.
    trigger = workflow.get(True, workflow.get("on"))
    assert isinstance(trigger, dict), f"the workflow declares no trigger mapping: {trigger!r}"
    return trigger


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "the workflow declares no jobs"
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps") or []
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _build_job(workflow: dict[str, Any]) -> dict[str, Any]:
    """The job is identified by the release build it runs, not by its name."""

    names = sorted(
        name
        for name, job in _jobs(workflow).items()
        if any(PACKAGE_BUILD_COMMAND in str(step.get("run") or "") for step in _steps(job))
    )
    assert len(names) == 1, f"exactly one job must run {PACKAGE_BUILD_COMMAND}: {names}"
    return _jobs(workflow)[names[0]]


def _step_indexes(job: dict[str, Any], fragment: str) -> list[int]:
    return [
        index
        for index, step in enumerate(_steps(job))
        if fragment in str(step.get("run") or "")
    ]


def _gate_index(job: dict[str, Any]) -> int:
    gates = _step_indexes(job, GATE_SCRIPT)
    assert len(gates) == 1, f"the build job must run {GATE_SCRIPT} exactly once, found {len(gates)}"
    return gates[0]


def _gate_step(workflow: dict[str, Any]) -> dict[str, Any]:
    job = _build_job(workflow)
    return _steps(job)[_gate_index(job)]


def _assert_a_store_candidate_needs_a_dispatch(workflow: dict[str, Any]) -> None:
    """No trigger may start this workflow from repository movement alone."""

    triggers = _triggers(workflow)
    assert set(triggers) == {"workflow_dispatch"}, (
        "a Store candidate must be built from declared coordinates, not from a "
        f"repository event: {sorted(triggers)}"
    )


def _assert_every_coordinate_is_required_and_defaulted_nowhere(
    workflow: dict[str, Any],
) -> None:
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert sorted(inputs) == sorted(REQUIRED_INPUTS), sorted(inputs)
    for name, declaration in inputs.items():
        assert declaration.get("required") is True, f"{name} is not required"
        assert "default" not in declaration, (
            f"{name} carries a default, so a release can be dispatched without it"
        )


def _assert_the_gate_reads_each_coordinate_separately(workflow: dict[str, Any]) -> None:
    environment = _gate_step(workflow).get("env") or {}
    for name, expression in COORDINATE_ENVIRONMENT.items():
        assert environment.get(name) == expression, (
            f"the gate must read {name} from {expression}, not {environment.get(name)!r}"
        )
    supplied = [environment.get(name) for name in COORDINATE_ENVIRONMENT]
    assert len(set(supplied)) == len(supplied), (
        f"two release coordinates resolve to one value: {supplied}"
    )


def _assert_the_gate_names_the_retrieval_credential(workflow: dict[str, Any]) -> None:
    environment = _gate_step(workflow).get("env") or {}
    for name, expression in CREDENTIAL_ENVIRONMENT.items():
        assert environment.get(name) == expression, (
            f"the gate must read {name} from {expression}, not {environment.get(name)!r}"
        )


def _assert_the_gate_precedes_every_packaging_step(workflow: dict[str, Any]) -> None:
    job = _build_job(workflow)
    gate = _gate_index(job)
    packaging = _step_indexes(job, PACKAGE_BUILD_COMMAND)
    assert len(packaging) == 1, f"the build job must package exactly once: {packaging}"
    assert gate < packaging[0], (
        "the Legal bindings must be verified before packaging starts, not after "
        f"it: gate at {gate}, packaging at {packaging[0]}"
    )
    step = _steps(job)[gate]
    assert step.get("continue-on-error") in (None, False), (
        "a gate that continues on error cannot stop a release"
    )


def _assert_packaging_reads_only_the_verified_document(workflow: dict[str, Any]) -> None:
    job = _build_job(workflow)
    command = str(_steps(job)[_step_indexes(job, PACKAGE_BUILD_COMMAND)[0]].get("run"))
    assert "-LegalReleaseBindings" in command, (
        "the packaging step is not handed any Legal bindings, so the gate gates nothing"
    )
    assert "$env:LEGAL_BINDINGS_DOCUMENT" in command, (
        "the packaging step must read the document the gate staged, not another path"
    )
    staged = str((job.get("env") or {}).get("LEGAL_BINDINGS_DOCUMENT", ""))
    assert staged.startswith("${{ runner.temp }}/"), (
        f"the verified document must be staged on ephemeral runner storage: {staged!r}"
    )
    assert "github.workspace" not in staged, staged


def _assert_the_checked_out_tree_is_the_declared_revision(
    workflow: dict[str, Any],
) -> None:
    checkouts = [
        step
        for step in _steps(_build_job(workflow))
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1, f"the build job must check out exactly once: {len(checkouts)}"
    with_block = checkouts[0].get("with") or {}
    assert with_block.get("ref") == "${{ inputs.product_revision }}", (
        "the build must check out the declared Product revision, not the ref the "
        f"run happens to sit on: {with_block.get('ref')!r}"
    )


def _assert_no_upload_carries_the_staged_document(workflow: dict[str, Any]) -> None:
    for job in _jobs(workflow).values():
        for step in _steps(job):
            if not str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                continue
            path = str((step.get("with") or {}).get("path", ""))
            assert "LEGAL_BINDINGS_DOCUMENT" not in path, (
                f"an artifact upload carries the Legal bindings document: {path}"
            )
            assert "legal-release-bindings" not in path, (
                f"an artifact upload carries the Legal bindings document: {path}"
            )
            assert "PACKAGED_SIGNING_RULE" not in path, (
                f"an artifact upload carries the packaged signing rule: {path}"
            )
            assert "packaged-signing-rule" not in path, (
                f"an artifact upload carries the packaged signing rule: {path}"
            )


def test_a_repository_event_can_no_longer_produce_a_store_candidate() -> None:
    """Restoring the tag trigger turns the named check red."""

    workflow = _workflow_document()
    _assert_a_store_candidate_needs_a_dispatch(workflow)

    tagged = deepcopy(workflow)
    tagged[True] = {**_triggers(tagged), "push": {"tags": ["v*"]}}
    with pytest.raises(AssertionError, match="not from a repository event"):
        _assert_a_store_candidate_needs_a_dispatch(tagged)


def test_every_release_coordinate_is_required_and_carries_no_default() -> None:
    """A default, or a dropped input, turns the named check red."""

    workflow = _workflow_document()
    _assert_every_coordinate_is_required_and_defaulted_nowhere(workflow)

    for name in REQUIRED_INPUTS:
        defaulted = deepcopy(workflow)
        _triggers(defaulted)["workflow_dispatch"]["inputs"][name]["default"] = "main"
        with pytest.raises(AssertionError, match="carries a default"):
            _assert_every_coordinate_is_required_and_defaulted_nowhere(defaulted)

        optional = deepcopy(workflow)
        _triggers(optional)["workflow_dispatch"]["inputs"][name]["required"] = False
        with pytest.raises(AssertionError, match="is not required"):
            _assert_every_coordinate_is_required_and_defaulted_nowhere(optional)

        dropped = deepcopy(workflow)
        del _triggers(dropped)["workflow_dispatch"]["inputs"][name]
        with pytest.raises(AssertionError):
            _assert_every_coordinate_is_required_and_defaulted_nowhere(dropped)


def test_the_two_legal_revisions_reach_the_gate_as_separate_coordinates() -> None:
    """Collapsing the fetch revision onto the source revision turns the named
    check red, which is what a workflow requiring the two to be equal does."""

    workflow = _workflow_document()
    _assert_the_gate_reads_each_coordinate_separately(workflow)

    collapsed = deepcopy(workflow)
    _gate_step(collapsed)["env"]["LEGAL_BINDINGS_REPOSITORY_REVISION"] = (
        "${{ inputs.legal_repository_revision }}"
    )
    with pytest.raises(AssertionError, match="must read LEGAL_BINDINGS_REPOSITORY_REVISION"):
        _assert_the_gate_reads_each_coordinate_separately(collapsed)


def test_the_gate_names_the_credential_it_fails_closed_without() -> None:
    """Dropping a secret from the gate step turns the named check red."""

    workflow = _workflow_document()
    _assert_the_gate_names_the_retrieval_credential(workflow)

    for name in CREDENTIAL_ENVIRONMENT:
        unbound = deepcopy(workflow)
        del _gate_step(unbound)["env"][name]
        with pytest.raises(AssertionError, match=f"must read {name}"):
            _assert_the_gate_names_the_retrieval_credential(unbound)


def test_the_bindings_are_verified_before_packaging_starts() -> None:
    """Moving the gate after packaging, deleting it, or letting it continue on
    error turns the named check red."""

    workflow = _workflow_document()
    _assert_the_gate_precedes_every_packaging_step(workflow)

    job_name = next(
        name
        for name, job in _jobs(workflow).items()
        if any(PACKAGE_BUILD_COMMAND in str(step.get("run") or "") for step in _steps(job))
    )

    reordered = deepcopy(workflow)
    steps = _jobs(reordered)[job_name]["steps"]
    gate = _gate_index(_build_job(reordered))
    packaging = _step_indexes(_build_job(reordered), PACKAGE_BUILD_COMMAND)[0]
    steps.insert(packaging, steps.pop(gate))
    with pytest.raises(AssertionError, match="before packaging starts"):
        _assert_the_gate_precedes_every_packaging_step(reordered)

    ungated = deepcopy(workflow)
    _jobs(ungated)[job_name]["steps"].remove(_gate_step(ungated))
    with pytest.raises(AssertionError, match="exactly once, found 0"):
        _assert_the_gate_precedes_every_packaging_step(ungated)

    tolerated = deepcopy(workflow)
    _gate_step(tolerated)["continue-on-error"] = True
    with pytest.raises(AssertionError, match="continues on error"):
        _assert_the_gate_precedes_every_packaging_step(tolerated)


def test_packaging_reads_the_document_the_gate_staged() -> None:
    """Dropping the switch, or staging into the workspace, turns the named check red."""

    workflow = _workflow_document()
    _assert_packaging_reads_only_the_verified_document(workflow)

    unbound = deepcopy(workflow)
    job = _build_job(unbound)
    index = _step_indexes(job, PACKAGE_BUILD_COMMAND)[0]
    _steps(job)[index]["run"] = str(_steps(job)[index]["run"]).replace(
        '-LegalReleaseBindings "$env:LEGAL_BINDINGS_DOCUMENT"', ""
    )
    with pytest.raises(AssertionError, match="not handed any Legal bindings"):
        _assert_packaging_reads_only_the_verified_document(unbound)

    checked_in = deepcopy(workflow)
    _build_job(checked_in)["env"]["LEGAL_BINDINGS_DOCUMENT"] = (
        "${{ github.workspace }}/legal-release-bindings.json"
    )
    with pytest.raises(AssertionError, match="ephemeral runner storage"):
        _assert_packaging_reads_only_the_verified_document(checked_in)


def test_the_build_packages_the_declared_product_revision() -> None:
    """Checking out the dispatch ref instead turns the named check red."""

    workflow = _workflow_document()
    _assert_the_checked_out_tree_is_the_declared_revision(workflow)

    floating = deepcopy(workflow)
    checkout = next(
        step
        for step in _steps(_build_job(floating))
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    checkout["with"]["ref"] = "${{ github.ref }}"
    with pytest.raises(AssertionError, match="declared Product revision"):
        _assert_the_checked_out_tree_is_the_declared_revision(floating)


def test_no_artifact_carries_the_legal_bindings_document() -> None:
    """Uploading the staged directory turns the named check red."""

    workflow = _workflow_document()
    _assert_no_upload_carries_the_staged_document(workflow)

    leaking = deepcopy(workflow)
    build = _build_job(leaking)
    upload = next(
        step
        for step in _steps(build)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    upload["with"]["path"] = "${{ env.LEGAL_BINDINGS_DOCUMENT }}"
    with pytest.raises(AssertionError, match="carries the Legal bindings document"):
        _assert_no_upload_carries_the_staged_document(leaking)
