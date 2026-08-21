"""Behavioural falsifiers for release signature verification, signing isolation, and
the packaged first-run release gate."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "packaging" / "verify-signatures.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-package.yml"
SIGNED_SYSTEM_BINARY = (
    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"
)

EXIT_VERIFIED = 0
EXIT_INPUT_REJECTED = 2
EXIT_SIGNATURE_REJECTED = 3
EXIT_TIMESTAMP_REJECTED = 4

# The two reads whose results decide every verdict. Replacing either with a
# constant produces a verifier that reports on something other than the file.
SIGNATURE_READ = "$signature = Get-AuthenticodeSignature -LiteralPath $file"
TIMESTAMP_READ = "$null -ne $signature.TimeStamperCertificate"

SIGNING_ACTION = "azure/trusted-signing-action"
PUBLISH_ACTION = "actions/upload-artifact"
VERIFICATION_STEP_ID = "verify-signatures"
VERIFICATION_COMMAND = ".\\packaging\\verify-signatures.ps1"
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")

# The signing job holds the federated credential, so what may run inside it is
# an allowlist: anything absent from these two sets is a new blast radius.
SIGNING_JOB_ACTIONS = frozenset(
    {
        "actions/checkout",
        "actions/download-artifact",
        "actions/upload-artifact",
        "azure/login",
        "azure/trusted-signing-action",
    }
)
SIGNING_JOB_COMMANDS = frozenset(
    {VERIFICATION_COMMAND, ".\\packaging\\write-digests.ps1"}
)

# The build job is identified by the release build it runs, and its first-run
# gate by the test that gate selects. That test opts out of itself unless the
# opt-in variable is set, and pytest.ini deselects the slow tier it belongs to,
# so the environment and the marker selection are both part of what makes the
# gate run at all.
PACKAGE_BUILD_COMMAND = ".\\packaging\\build-windows.ps1"
FIRST_RUN_GATE_TEST = (
    "tests/test_packaged_runtime.py"
    "::test_real_installed_launcher_starts_the_frozen_brain_and_owns_its_listener"
)
FIRST_RUN_GATE_OPT_IN = "BRAIN_INSTALLED_LAUNCHER_E2E"
FIRST_RUN_GATE_BUILD_PYTHON = "BRAIN_PACKAGED_BUILD_PYTHON"
BUILD_ENVIRONMENT_INTERPRETER = ".build-venv\\Scripts\\python.exe"


# The workflow runs the verifier under `shell: pwsh`; Windows PowerShell is the
# fallback for a machine without it.
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell.exe"


def _run_verifier(
    path: Path | None, *, script: Path = VERIFY_SCRIPT
) -> subprocess.CompletedProcess[str]:
    command = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    if path is not None:
        command.extend(("-Path", str(path)))
    # An inherited PSModulePath can point a child host at another edition's
    # modules, which makes Get-AuthenticodeSignature unresolvable. Dropping it
    # lets the child compute its own default.
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() != "PSMODULEPATH"
    }
    return subprocess.run(
        command, capture_output=True, text=True, check=False, env=environment
    )


def _script_variant(
    tmp_path: Path,
    name: str,
    original: str,
    replacement: str,
    *,
    script: Path = VERIFY_SCRIPT,
) -> Path:
    """Copy a verifier with one expression replaced.

    The replacement is not asserted to apply. A verifier that no longer contains
    the expression must fail the behavioural assertion that follows, not a check
    on its own text.
    """

    variant = tmp_path / name
    variant.write_text(
        script.read_text(encoding="utf-8").replace(original, replacement),
        encoding="utf-8",
    )
    return variant


def _assert_signature_accepted(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == EXIT_VERIFIED, (
        "the verifier must accept a file carrying a valid signature: "
        f"exit {result.returncode} {result.stdout}{result.stderr}"
    )
    assert "signature check accepted" in result.stdout


def _assert_signature_rejected(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == EXIT_SIGNATURE_REJECTED, (
        "the verifier must reject an unsigned file at the signature check: "
        f"exit {result.returncode} {result.stdout}{result.stderr}"
    )
    assert "signature check rejected" in result.stdout


def _assert_input_rejected(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == EXIT_INPUT_REJECTED, (
        "the verifier must reject an input that resolves to no file: "
        f"exit {result.returncode} {result.stdout}{result.stderr}"
    )
    assert "input rejected" in result.stdout


def _assert_the_timestamp_decides_the_verdict(
    tmp_path: Path, *, script: Path = VERIFY_SCRIPT
) -> None:
    """A validly signed file's verdict follows its countersignature timestamp.

    Every signed binary on a stock Windows machine is timestamped, and producing
    a signed-but-untimestamped one would mean trusting a certificate, so the
    dependency is shown from the other side: a copy of the verifier whose
    timestamp read reports the certificate absent rejects the same signed binary
    at the timestamp check. A verifier that assumes a timestamp instead of
    reading one cannot produce that rejection.
    """

    probe = _script_variant(
        tmp_path,
        "verify-signatures-untimestamped.ps1",
        TIMESTAMP_READ,
        "$false",
        script=script,
    )
    result = _run_verifier(SIGNED_SYSTEM_BINARY, script=probe)
    assert result.returncode == EXIT_TIMESTAMP_REJECTED, (
        "the verifier must reject a signature that carries no timestamp, and "
        f"reject it at the timestamp check: exit {result.returncode} "
        f"{result.stdout}{result.stderr}"
    )
    assert "timestamp check rejected" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Authenticode verification is Windows-only")
def test_verifier_separates_a_signed_binary_from_unsigned_bytes(tmp_path: Path) -> None:
    """A verifier that reports a stub instead of reading the file turns the check red."""

    assert SIGNED_SYSTEM_BINARY.is_file(), (
        f"the signed reference binary is absent: {SIGNED_SYSTEM_BINARY}"
    )
    _assert_signature_accepted(_run_verifier(SIGNED_SYSTEM_BINARY))

    unsigned = tmp_path / "unsigned.exe"
    unsigned.write_bytes(b"frozen-brain")
    _assert_signature_rejected(_run_verifier(unsigned))

    # Byte-identical bytes at a different path stay accepted, and one changed
    # byte is rejected, so the verdict follows the file's own signature.
    signed_bytes = SIGNED_SYSTEM_BINARY.read_bytes()
    intact = tmp_path / "intact"
    intact.mkdir()
    copied = intact / SIGNED_SYSTEM_BINARY.name
    copied.write_bytes(signed_bytes)
    _assert_signature_accepted(_run_verifier(copied))

    perturbed = bytearray(signed_bytes)
    offset = len(perturbed) // 2
    perturbed[offset] = (perturbed[offset] + 1) % 256
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    tampered_copy = tampered / SIGNED_SYSTEM_BINARY.name
    tampered_copy.write_bytes(bytes(perturbed))
    _assert_signature_rejected(_run_verifier(tampered_copy))

    unread = _script_variant(
        tmp_path,
        "verify-signatures-unread.ps1",
        SIGNATURE_READ,
        '$signature = [pscustomobject]@{ Status = "Valid"; TimeStamperCertificate = "unread" }',
    )
    permitted = _run_verifier(unsigned, script=unread)
    with pytest.raises(AssertionError, match="must reject an unsigned file"):
        _assert_signature_rejected(permitted)
    assert permitted.returncode == EXIT_VERIFIED, (
        f"exit {permitted.returncode} {permitted.stdout}{permitted.stderr}"
    )


@pytest.mark.skipif(os.name != "nt", reason="Authenticode verification is Windows-only")
def test_verifier_requires_a_countersignature_timestamp(tmp_path: Path) -> None:
    """A verifier that assumes a timestamp rather than reading one turns the check red."""

    assert SIGNED_SYSTEM_BINARY.is_file()
    accepted = _run_verifier(SIGNED_SYSTEM_BINARY)
    _assert_signature_accepted(accepted)
    assert "timestamped=True" in accepted.stdout

    _assert_the_timestamp_decides_the_verdict(tmp_path)

    assumed = _script_variant(
        tmp_path,
        "verify-signatures-assumed-timestamp.ps1",
        TIMESTAMP_READ,
        "$true",
    )
    with pytest.raises(
        AssertionError, match="must reject a signature that carries no timestamp"
    ):
        _assert_the_timestamp_decides_the_verdict(tmp_path, script=assumed)


@pytest.mark.skipif(os.name != "nt", reason="Authenticode verification is Windows-only")
def test_verifier_rejects_an_input_that_resolves_to_no_file(tmp_path: Path) -> None:
    """No named path, an absent path, and a directory of unsignable files all refuse."""

    _assert_input_rejected(_run_verifier(None))
    _assert_input_rejected(_run_verifier(tmp_path / "absent.exe"))

    unsignable = tmp_path / "unsignable"
    unsignable.mkdir()
    (unsignable / "Agent-chrome.zip").write_bytes(b"archive bytes")
    (unsignable / "sha256sums.txt").write_text("", encoding="ascii")
    _assert_input_rejected(_run_verifier(unsignable))


@pytest.mark.skipif(os.name != "nt", reason="Authenticode verification is Windows-only")
def test_verifier_reads_a_release_directory_and_follows_its_executable(
    tmp_path: Path,
) -> None:
    """The published set verifies through its installer, and an unsigned installer refuses."""

    assert SIGNED_SYSTEM_BINARY.is_file()
    release = tmp_path / "release"
    release.mkdir()
    (release / "OnlyFans-Conversational-Analytics-Agent-1.0.0-chrome.zip").write_bytes(
        b"archive bytes"
    )
    (release / "sha256sums.txt").write_text("", encoding="ascii")
    installer = release / "OnlyFans-Conversational-Analytics-Setup-1.0.0-x64.exe"
    installer.write_bytes(SIGNED_SYSTEM_BINARY.read_bytes())
    _assert_signature_accepted(_run_verifier(release))

    installer.write_bytes(b"unsigned installer bytes")
    _assert_signature_rejected(_run_verifier(release))


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "the workflow declares no jobs"
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps") or []
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _action_path(step: dict[str, Any]) -> str | None:
    uses = step.get("uses")
    return uses.partition("@")[0] if isinstance(uses, str) else None


def _grants_the_signing_token(permissions: Any) -> bool:
    if permissions == "write-all":
        return True
    return isinstance(permissions, dict) and permissions.get("id-token") == "write"


def _signing_job_name(workflow: dict[str, Any]) -> str:
    """The job is identified by the signing action it runs, not by its name."""

    names = sorted(
        name
        for name, job in _jobs(workflow).items()
        if any(_action_path(step) == SIGNING_ACTION for step in _steps(job))
    )
    assert len(names) == 1, f"exactly one job must run {SIGNING_ACTION}: {names}"
    return names[0]


def _signing_job(workflow: dict[str, Any]) -> dict[str, Any]:
    return _jobs(workflow)[_signing_job_name(workflow)]


def _assert_only_the_signing_job_holds_the_token(workflow: dict[str, Any]) -> None:
    assert not _grants_the_signing_token(workflow.get("permissions")), (
        "id-token: write must not be granted at workflow level"
    )
    holders = sorted(
        name
        for name, job in _jobs(workflow).items()
        if _grants_the_signing_token(job.get("permissions"))
    )
    assert holders == [_signing_job_name(workflow)], (
        f"only the signing job may hold id-token: write, not {holders}"
    )


def _assert_the_signing_job_declares_the_release_environment(
    workflow: dict[str, Any],
) -> None:
    environment = _signing_job(workflow).get("environment")
    declared = environment.get("name") if isinstance(environment, dict) else environment
    assert declared == "release", (
        f"the signing job must declare environment: release, not {environment!r}"
    )


def _assert_the_signing_job_installs_and_builds_nothing(
    workflow: dict[str, Any],
) -> None:
    steps = _steps(_signing_job(workflow))
    assert steps, "the signing job declares no step"
    for index, step in enumerate(steps):
        action = _action_path(step)
        if action is not None:
            assert action in SIGNING_JOB_ACTIONS, (
                f"signing job step {index} runs an action outside the allowlist: {action}"
            )
            continue
        script = step.get("run")
        assert isinstance(script, str), (
            f"signing job step {index} neither uses an action nor runs a command"
        )
        commands = [line.strip() for line in script.splitlines() if line.strip()]
        assert commands, f"signing job step {index} runs an empty command"
        for command in commands:
            assert command.split()[0] in SIGNING_JOB_COMMANDS, (
                f"signing job step {index} runs a command outside the allowlist: {command}"
            )


def _assert_every_signing_job_action_is_pinned_to_a_commit(
    workflow: dict[str, Any],
) -> None:
    references = [
        step["uses"]
        for step in _steps(_signing_job(workflow))
        if isinstance(step.get("uses"), str)
    ]
    assert references, "the signing job uses no action, so nothing was checked"
    unpinned = sorted(
        reference
        for reference in references
        if not COMMIT_SHA.fullmatch(reference.partition("@")[2])
    )
    assert not unpinned, (
        f"every signing-job action must be pinned to a 40-character commit: {unpinned}"
    )


def _assert_publication_is_downstream_of_verification(
    workflow: dict[str, Any],
) -> None:
    steps = _steps(_signing_job(workflow))
    verification = [
        index
        for index, step in enumerate(steps)
        if step.get("id") == VERIFICATION_STEP_ID
    ]
    assert len(verification) == 1, (
        f"the signing job must contain exactly one {VERIFICATION_STEP_ID} step, "
        f"found {len(verification)}"
    )
    script = steps[verification[0]].get("run")
    assert isinstance(script, str) and script.split()[0] == VERIFICATION_COMMAND, (
        f"the {VERIFICATION_STEP_ID} step must run {VERIFICATION_COMMAND}, not {script!r}"
    )
    publication = [
        index
        for index, step in enumerate(steps)
        if _action_path(step) == PUBLISH_ACTION
    ]
    assert len(publication) == 1, (
        f"the signing job must publish exactly once, found {len(publication)}"
    )
    assert verification[0] < publication[0], (
        "the publishing step must come after the verification step"
    )
    condition = steps[publication[0]].get("if")
    assert isinstance(condition, str) and f"steps.{VERIFICATION_STEP_ID}" in condition, (
        "the publishing step must be conditional on the verification step, "
        f"not {condition!r}"
    )


def test_only_the_signing_job_holds_the_federated_token() -> None:
    """Granting the token to the build job, or workflow-wide, turns the named check red."""

    workflow = _workflow_document()
    _assert_only_the_signing_job_holds_the_token(workflow)

    shared = deepcopy(workflow)
    signing = _signing_job_name(shared)
    other = sorted(name for name in _jobs(shared) if name != signing)
    assert other, "the workflow must declare a job other than the signing job"
    _jobs(shared)[other[0]].setdefault("permissions", {})["id-token"] = "write"
    with pytest.raises(AssertionError, match="only the signing job may hold"):
        _assert_only_the_signing_job_holds_the_token(shared)

    hoisted = deepcopy(workflow)
    hoisted["permissions"] = {"contents": "read", "id-token": "write"}
    with pytest.raises(AssertionError, match="must not be granted at workflow level"):
        _assert_only_the_signing_job_holds_the_token(hoisted)


def test_the_signing_job_runs_in_the_release_environment() -> None:
    """Dropping the environment declaration turns the named check red."""

    workflow = _workflow_document()
    _assert_the_signing_job_declares_the_release_environment(workflow)

    unbound = deepcopy(workflow)
    _signing_job(unbound).pop("environment")
    with pytest.raises(AssertionError, match="must declare environment: release"):
        _assert_the_signing_job_declares_the_release_environment(unbound)


def test_the_signing_job_installs_nothing_and_builds_nothing() -> None:
    """A dependency install, or a toolchain action, turns the named check red."""

    workflow = _workflow_document()
    _assert_the_signing_job_installs_and_builds_nothing(workflow)

    installing = deepcopy(workflow)
    _signing_job(installing)["steps"].insert(
        1,
        {
            "name": "Install backend dependencies",
            "run": "python -m pip install -r requirements.txt",
        },
    )
    with pytest.raises(AssertionError, match="runs a command outside the allowlist"):
        _assert_the_signing_job_installs_and_builds_nothing(installing)

    provisioning = deepcopy(workflow)
    _signing_job(provisioning)["steps"].insert(
        1,
        {
            "name": "Setup Python",
            "uses": "actions/setup-python@" + "0" * 40,
            "with": {"python-version": "3.11"},
        },
    )
    with pytest.raises(AssertionError, match="runs an action outside the allowlist"):
        _assert_the_signing_job_installs_and_builds_nothing(provisioning)


def test_the_signing_job_pins_every_action_to_a_commit() -> None:
    """Restoring a movable major tag on any signing-job action turns the named check red."""

    workflow = _workflow_document()
    _assert_every_signing_job_action_is_pinned_to_a_commit(workflow)

    for index, step in enumerate(_steps(_signing_job(workflow))):
        if not isinstance(step.get("uses"), str):
            continue
        movable = deepcopy(workflow)
        action = _action_path(step)
        _signing_job(movable)["steps"][index]["uses"] = f"{action}@v4"
        with pytest.raises(AssertionError, match="pinned to a 40-character commit"):
            _assert_every_signing_job_action_is_pinned_to_a_commit(movable)


def test_publication_cannot_precede_verification() -> None:
    """Moving the publish step ahead of verification, or deleting it, turns the named check red."""

    workflow = _workflow_document()
    _assert_publication_is_downstream_of_verification(workflow)

    reordered = deepcopy(workflow)
    steps = _signing_job(reordered)["steps"]
    publication = next(
        index for index, step in enumerate(steps) if _action_path(step) == PUBLISH_ACTION
    )
    verification = next(
        index for index, step in enumerate(steps) if step.get("id") == VERIFICATION_STEP_ID
    )
    steps.insert(verification, steps.pop(publication))
    with pytest.raises(AssertionError, match="must come after the verification step"):
        _assert_publication_is_downstream_of_verification(reordered)

    unverified = deepcopy(workflow)
    steps = _signing_job(unverified)["steps"]
    steps.pop(
        next(
            index
            for index, step in enumerate(steps)
            if step.get("id") == VERIFICATION_STEP_ID
        )
    )
    with pytest.raises(AssertionError, match="exactly one verify-signatures step"):
        _assert_publication_is_downstream_of_verification(unverified)


def _build_job_name(workflow: dict[str, Any]) -> str:
    """The build job is identified by the release build it runs, not by its name."""

    names = sorted(
        name
        for name, job in _jobs(workflow).items()
        if any(
            PACKAGE_BUILD_COMMAND in str(step.get("run") or "") for step in _steps(job)
        )
    )
    assert len(names) == 1, f"exactly one job must run {PACKAGE_BUILD_COMMAND}: {names}"
    return names[0]


def _build_job(workflow: dict[str, Any]) -> dict[str, Any]:
    return _jobs(workflow)[_build_job_name(workflow)]


def _first_run_gate_index(steps: list[dict[str, Any]]) -> int:
    gates = [
        index
        for index, step in enumerate(steps)
        if FIRST_RUN_GATE_TEST in str(step.get("run") or "")
    ]
    assert len(gates) == 1, (
        f"the build job must run {FIRST_RUN_GATE_TEST} exactly once, "
        f"found {len(gates)}"
    )
    return gates[0]


def _first_run_gate_step(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = _steps(_build_job(workflow))
    return steps[_first_run_gate_index(steps)]


def _pytest_arguments(step: dict[str, Any]) -> list[str]:
    """What the gate hands pytest, which is what decides the selection."""

    command = step.get("run")
    assert isinstance(command, str), "the packaged first-run gate runs no command"
    tokens = command.split()
    assert "pytest" in tokens, (
        f"the packaged first-run gate does not run pytest: {command!r}"
    )
    return tokens[tokens.index("pytest") + 1 :]


def _without_marker_selection(arguments: list[str]) -> list[str]:
    remaining = list(arguments)
    assert "-m" in remaining, (
        "the packaged first-run gate must select a marker explicitly, because "
        "pytest.ini deselects the tier the test belongs to"
    )
    index = remaining.index("-m")
    del remaining[index : index + 2]
    return remaining


def _collect(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _collected_node_ids(
    collected: subprocess.CompletedProcess[str],
) -> list[str]:
    return [line.strip() for line in collected.stdout.splitlines() if "::" in line]


def _assert_the_packaged_first_run_gates_the_upload(workflow: dict[str, Any]) -> None:
    steps = _steps(_build_job(workflow))
    gate = _first_run_gate_index(steps)
    environment = steps[gate].get("env") or {}
    assert str(environment.get(FIRST_RUN_GATE_OPT_IN)) == "1", (
        f"the gate must set {FIRST_RUN_GATE_OPT_IN}=1, or the test skips itself: "
        f"{environment.get(FIRST_RUN_GATE_OPT_IN)!r}"
    )
    interpreter = str(environment.get(FIRST_RUN_GATE_BUILD_PYTHON, ""))
    assert interpreter.endswith(BUILD_ENVIRONMENT_INTERPRETER), (
        "the gate must freeze with the job's isolated build environment in "
        f"{FIRST_RUN_GATE_BUILD_PYTHON}, not {interpreter!r}"
    )
    publication = [
        index
        for index, step in enumerate(steps)
        if _action_path(step) == PUBLISH_ACTION
    ]
    assert len(publication) == 1, (
        f"the build job must upload exactly once, found {len(publication)}"
    )
    assert gate < publication[0], (
        "the packaged first-run gate must run before the package is uploaded"
    )


def test_the_build_job_gates_the_upload_on_a_packaged_first_run() -> None:
    """Deleting the gate, its opt-in, its build environment, or its position ahead
    of the upload turns the named check red."""

    workflow = _workflow_document()
    _assert_the_packaged_first_run_gates_the_upload(workflow)

    ungated = deepcopy(workflow)
    _build_job(ungated)["steps"].remove(_first_run_gate_step(ungated))
    with pytest.raises(AssertionError, match="exactly once, found 0"):
        _assert_the_packaged_first_run_gates_the_upload(ungated)

    unopted = deepcopy(workflow)
    _first_run_gate_step(unopted)["env"].pop(FIRST_RUN_GATE_OPT_IN)
    with pytest.raises(AssertionError, match=f"must set {FIRST_RUN_GATE_OPT_IN}=1"):
        _assert_the_packaged_first_run_gates_the_upload(unopted)

    detached = deepcopy(workflow)
    _first_run_gate_step(detached)["env"][FIRST_RUN_GATE_BUILD_PYTHON] = (
        "${{ github.workspace }}\\.other-venv\\Scripts\\python.exe"
    )
    with pytest.raises(AssertionError, match="isolated build environment"):
        _assert_the_packaged_first_run_gates_the_upload(detached)

    reordered = deepcopy(workflow)
    steps = _build_job(reordered)["steps"]
    gate = _first_run_gate_index(_steps(_build_job(reordered)))
    publication = next(
        index
        for index, step in enumerate(steps)
        if _action_path(step) == PUBLISH_ACTION
    )
    steps.insert(gate, steps.pop(publication))
    with pytest.raises(AssertionError, match="before the package is uploaded"):
        _assert_the_packaged_first_run_gates_the_upload(reordered)


def test_the_gate_selection_collects_the_packaged_first_run_test() -> None:
    """The gate's own arguments are handed to pytest: dropping the marker
    selection collects nothing, so the release would ship unexercised."""

    arguments = _pytest_arguments(_first_run_gate_step(_workflow_document()))

    selected = _collect(arguments)
    assert _collected_node_ids(selected) == [FIRST_RUN_GATE_TEST], (
        selected.stdout + selected.stderr
    )

    deselected = _collect(_without_marker_selection(arguments))
    assert _collected_node_ids(deselected) == [], deselected.stdout + deselected.stderr
