"""Behavioural falsifiers for the release workflow's own packaging invocation.

Every packaging test drives ``packaging/build-windows.ps1`` with an argument
list it writes itself, so all of them passed while the one invocation that
builds a Store candidate omitted a release input the script refuses to build
without. These checks read the argument list out of the workflow and the
required inputs out of the script, and assert the first satisfies the second.

Each check is asserted against the two files as written and then against a copy
with one thing changed, on both sides, so a check that could only ever pass is
visible as one that no mutation turns red.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-package.yml"
SCRIPT = ROOT / "packaging" / "build-windows.ps1"
LEGAL_GATE = ROOT / "tools" / "legal-release-bindings" / "verify.mjs"

GATE_SCRIPT = "tools/legal-release-bindings/verify.mjs"
PACKAGE_BUILD_COMMAND = ".\\packaging\\build-windows.ps1"

# A PowerShell parameter starts a token; a hyphen inside one is part of a value.
PARAMETER = re.compile(r"(?:(?<=\s)|\A)-(?P<name>[A-Za-z][A-Za-z0-9]*)\b")

# The script's own param block, and the typed declarations inside it.
PARAM_BLOCK = re.compile(r"^param\(\n(?P<body>.*?)^\)$", re.DOTALL | re.MULTILINE)
TYPED_PARAMETER = re.compile(r"^\[(?P<type>[A-Za-z]+)\]\s*\$(?P<name>[A-Za-z][A-Za-z0-9]*)")
MANDATORY_ATTRIBUTE = "[Parameter(Mandatory"

# The block that decides what a release may not be built without. Its members
# are read from the script rather than restated here, so an input added there
# without being passed here is what turns these checks red.
RELEASE_BLOCK = re.compile(r"^if \(\$ReleaseMode\) \{\n(?P<body>.*?)^\}$", re.DOTALL | re.MULTILINE)
RELEASE_INPUT = re.compile(r'-Name\s+"(?P<name>[A-Za-z][A-Za-z0-9]*)"')

# The switch that turns release mode off. A Store candidate is built with it
# absent, so the workflow that mints one may never pass it.
DEVELOPMENT_SWITCH = "DevelopmentAgentBundle"

# The name the bindings gate emits its derived privacy policy URL under.
EXPORTED_URL_VARIABLE = re.compile(
    r"PRIVACY_POLICY_URL_VARIABLE\s*=\s*'(?P<name>[A-Z][A-Z0-9_]*)'"
)
ENVIRONMENT_REFERENCE = re.compile(r'^"?\$env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)"?$')

# A name published into the job environment file, as the assignment a step
# writes there rather than as the variable a later step reads back.
ENVIRONMENT_FILE_WRITE = re.compile(r'"(?P<name>[A-Z][A-Z0-9_]*)=[^"\n]*"')

# Values a release input may never be written down as in the workflow itself.
LITERAL_URL = re.compile(r"https?://", re.IGNORECASE)


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


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


def _package_step(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = [
        step
        for step in _steps(_build_job(workflow))
        if PACKAGE_BUILD_COMMAND in str(step.get("run") or "")
    ]
    assert len(steps) == 1, f"the build job must package exactly once: {len(steps)}"
    return steps[0]


def _gate_step(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = [
        step
        for step in _steps(_build_job(workflow))
        if GATE_SCRIPT in str(step.get("run") or "")
    ]
    assert len(steps) == 1, f"the build job must run {GATE_SCRIPT} exactly once: {len(steps)}"
    return steps[0]


def _package_command(workflow: dict[str, Any]) -> str:
    command = str(_package_step(workflow).get("run") or "")
    assert command.count(PACKAGE_BUILD_COMMAND) == 1, (
        f"the packaging step must invoke the script once: {command!r}"
    )
    return command[command.index(PACKAGE_BUILD_COMMAND):]


def _passed_arguments(command: str) -> dict[str, str]:
    """The parameters the workflow passes, mapped to the values it passes them."""

    matches = list(PARAMETER.finditer(command))
    arguments: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(command)
        value = command[match.end():end].strip().rstrip("`").strip()
        arguments[match.group("name")] = value
    return arguments


def _declared_parameters(script: str) -> dict[str, bool]:
    """Every parameter the script declares, mapped to whether it is mandatory."""

    block = PARAM_BLOCK.search(script)
    assert block is not None, "the packaging script declares no param block"
    declared: dict[str, bool] = {}
    mandatory = False
    for line in block.group("body").splitlines():
        stripped = line.strip()
        if stripped.startswith(MANDATORY_ATTRIBUTE):
            mandatory = True
            continue
        typed = TYPED_PARAMETER.match(stripped)
        if typed is None:
            continue
        declared[typed.group("name")] = mandatory
        mandatory = False
    assert declared, "no parameter declaration was read from the packaging script"
    return declared


def _release_inputs(script: str) -> set[str]:
    """The inputs the script refuses to build a Store candidate without."""

    block = RELEASE_BLOCK.search(script)
    assert block is not None, "the packaging script declares no release-mode block"
    names = {match.group("name") for match in RELEASE_INPUT.finditer(block.group("body"))}
    assert names, "no release input was read from the packaging script"
    return names


def _demanded_parameters(script: str) -> set[str]:
    """Everything a release invocation must carry: mandatory plus release-only."""

    declared = _declared_parameters(script)
    return {name for name, mandatory in declared.items() if mandatory} | _release_inputs(script)


def _published_names(job: dict[str, Any]) -> set[str]:
    """Names a step writes into the job environment file.

    A job environment cannot read the runner context, so a path on ephemeral
    runner storage reaches the packaging step this way rather than from a
    job-level declaration.
    """

    published: set[str] = set()
    for step in _steps(job):
        run = str(step.get("run") or "")
        if "GITHUB_ENV" not in run:
            continue
        published |= {match.group("name") for match in ENVIRONMENT_FILE_WRITE.finditer(run)}
    return published


def _exported_url_variable() -> str:
    match = EXPORTED_URL_VARIABLE.search(LEGAL_GATE.read_text(encoding="utf-8"))
    assert match is not None, f"{LEGAL_GATE} names no derived privacy policy variable"
    return match.group("name")


def _with_extra_release_input(script: str, name: str) -> str:
    block = RELEASE_BLOCK.search(script)
    assert block is not None
    injected = f'    $Extra = Resolve-ReleaseInput -Value $Extra -Name "{name}"\n'
    return script[: block.end("body")] + injected + script[block.end("body"):]


def _assert_the_invocation_satisfies_every_release_input(
    workflow: dict[str, Any],
    script: str,
) -> None:
    """The workflow must pass every input the script demands in release mode."""

    passed = set(_passed_arguments(_package_command(workflow)))
    missing = sorted(_demanded_parameters(script) - passed)
    assert not missing, (
        "the release workflow invokes packaging/build-windows.ps1 without an "
        f"input a release build demands, so it throws before building anything: "
        f"{', '.join(missing)}"
    )


def _assert_the_invocation_passes_nothing_the_script_does_not_declare(
    workflow: dict[str, Any],
    script: str,
) -> None:
    passed = set(_passed_arguments(_package_command(workflow)))
    unknown = sorted(passed - set(_declared_parameters(script)))
    assert not unknown, (
        "the release workflow passes a parameter packaging/build-windows.ps1 "
        f"does not declare: {', '.join(unknown)}"
    )


def _assert_every_release_input_the_script_names_is_declared(script: str) -> None:
    undeclared = sorted(_release_inputs(script) - set(_declared_parameters(script)))
    assert not undeclared, (
        "the release-mode block demands an input the param block does not "
        f"declare: {', '.join(undeclared)}"
    )


def _assert_the_workflow_builds_a_store_candidate(workflow: dict[str, Any]) -> None:
    passed = set(_passed_arguments(_package_command(workflow)))
    assert DEVELOPMENT_SWITCH not in passed, (
        f"the release workflow passes -{DEVELOPMENT_SWITCH}, so it publishes a "
        "development bundle and mints no Store candidate"
    )


def _assert_no_release_input_is_written_down(
    workflow: dict[str, Any],
    script: str,
) -> None:
    """A release input must name a verified value, never carry one.

    Each of them is either a document a gate staged or a value a gate derived,
    so the workflow may only reference the variable it was published under. A
    literal here would be a coordinate nothing verified.
    """

    job = _build_job(workflow)
    arguments = _passed_arguments(_package_command(workflow))
    staged = set(job.get("env") or {}) | _published_names(job)
    derived = {_exported_url_variable()}
    for name in sorted(_release_inputs(script)):
        value = arguments.get(name, "")
        reference = ENVIRONMENT_REFERENCE.match(value)
        assert reference is not None, (
            f"-{name} is written down in the workflow rather than read from a "
            f"verified value: {value!r}"
        )
        assert not LITERAL_URL.search(value), value
        variable = reference.group("name")
        assert variable in staged | derived, (
            f"-{name} reads {variable}, which no gate in this job stages or "
            f"derives: staged {sorted(staged)}, derived {sorted(derived)}"
        )


def _assert_the_derived_url_is_published_by_the_gate(workflow: dict[str, Any]) -> None:
    """The gate must be asked for the value the packaging step reads back."""

    command = str(_gate_step(workflow).get("run") or "")
    assert "--environment-file=" in command, (
        "the bindings gate is not asked to publish the URL it derives, so the "
        f"packaging step reads an unset variable: {command!r}"
    )
    variable = _exported_url_variable()
    passed = _passed_arguments(_package_command(workflow))
    assert any(
        ENVIRONMENT_REFERENCE.match(value) is not None
        and ENVIRONMENT_REFERENCE.match(value).group("name") == variable
        for value in passed.values()
    ), f"no packaging parameter reads {variable}, which the gate publishes"


def test_the_release_workflow_passes_every_input_the_script_demands() -> None:
    """Dropping a parameter from the workflow turns the named check red, and so
    does adding a release input to the script that the workflow does not pass."""

    workflow = _workflow_document()
    script = _script_text()
    _assert_the_invocation_satisfies_every_release_input(workflow, script)

    for name in sorted(_demanded_parameters(script)):
        dropped = deepcopy(workflow)
        step = _package_step(dropped)
        arguments = _passed_arguments(_package_command(dropped))
        step["run"] = str(step["run"]).replace(f"-{name} {arguments[name]}", "")
        with pytest.raises(AssertionError, match=name):
            _assert_the_invocation_satisfies_every_release_input(dropped, script)

    extended = _with_extra_release_input(script, "UnpassedReleaseInput")
    assert extended != script
    with pytest.raises(AssertionError, match="UnpassedReleaseInput"):
        _assert_the_invocation_satisfies_every_release_input(workflow, extended)


def test_the_release_workflow_passes_nothing_the_script_cannot_accept() -> None:
    """A misspelled switch, which PowerShell rejects at run time, turns the
    named check red before a release is dispatched."""

    workflow = _workflow_document()
    script = _script_text()
    _assert_the_invocation_passes_nothing_the_script_does_not_declare(workflow, script)
    _assert_every_release_input_the_script_names_is_declared(script)

    misspelled = deepcopy(workflow)
    step = _package_step(misspelled)
    step["run"] = str(step["run"]).replace("-PackagedSigningRule", "-PackagedSigningRuleFile")
    with pytest.raises(AssertionError, match="PackagedSigningRuleFile"):
        _assert_the_invocation_passes_nothing_the_script_does_not_declare(misspelled, script)


def test_the_release_workflow_mints_a_store_candidate() -> None:
    """Turning release mode off in the workflow turns the named check red."""

    workflow = _workflow_document()
    _assert_the_workflow_builds_a_store_candidate(workflow)

    development = deepcopy(workflow)
    step = _package_step(development)
    step["run"] = f"{step['run'].rstrip()} `\n            -{DEVELOPMENT_SWITCH}\n"
    with pytest.raises(AssertionError, match=DEVELOPMENT_SWITCH):
        _assert_the_workflow_builds_a_store_candidate(development)


def test_no_release_input_is_written_down_in_the_workflow() -> None:
    """Hard-coding a privacy policy URL, or reading a variable no gate produces,
    turns the named check red."""

    workflow = _workflow_document()
    script = _script_text()
    _assert_no_release_input_is_written_down(workflow, script)

    hard_coded = deepcopy(workflow)
    step = _package_step(hard_coded)
    step["run"] = str(step["run"]).replace(
        f'"$env:{_exported_url_variable()}"', '"https://example.test/legal/privacy"'
    )
    with pytest.raises(AssertionError, match="written down in the workflow"):
        _assert_no_release_input_is_written_down(hard_coded, script)

    unstaged = deepcopy(workflow)
    for step in _steps(_build_job(unstaged)):
        run = str(step.get("run") or "")
        if "GITHUB_ENV" in run:
            step["run"] = "\n".join(
                line
                for line in run.splitlines()
                if '"PACKAGED_SIGNING_RULE=' not in line
            )
    with pytest.raises(AssertionError, match="which no gate in this job stages"):
        _assert_no_release_input_is_written_down(unstaged, script)


def test_the_privacy_policy_url_is_the_one_the_bindings_gate_derives() -> None:
    """Dropping the gate's publication flag turns the named check red, which is
    what a workflow reading an unset variable would otherwise do silently."""

    workflow = _workflow_document()
    _assert_the_derived_url_is_published_by_the_gate(workflow)

    unpublished = deepcopy(workflow)
    step = _gate_step(unpublished)
    step["run"] = re.sub(r"\s*--environment-file=\S+", "", str(step["run"]))
    with pytest.raises(AssertionError, match="not asked to publish"):
        _assert_the_derived_url_is_published_by_the_gate(unpublished)
