from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.ci_tier("fast")]
ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
VISUAL_WORKFLOW = ROOT / ".github/workflows/visual-capture.yml"
PRODUCERS = ("visual-capture-execution", "visual-capture-serial-control")


def _load(path: Path = VISUAL_WORKFLOW) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_visual_capture_keeps_its_separate_stable_check() -> None:
    assert "visual-capture" not in _load(CI_WORKFLOW)["jobs"]
    workflow = _load()
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["env"]["VISUAL_CI_POLICY_VERSION"] == "visual-v1"
    for key in ("PRODUCT_SHA", "VISUAL_CAPTURE_REVISION"):
        assert workflow["env"][key] == "${{ github.event.pull_request.head.sha || github.sha }}"
    for job in workflow["jobs"].values():
        assert _step(job, "Checkout exact Product revision")["with"]["ref"] == "${{ env.PRODUCT_SHA }}"
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }


def _assert_all_changes_have_a_gate(workflow: dict[str, Any]) -> None:
    for event in ("push", "pull_request"):
        assert workflow["on"][event]["branches"] == ["main"]
        assert "paths" not in workflow["on"][event] and "paths-ignore" not in workflow["on"][event], "required visual feedback must not wait on a filtered workflow"
    assert set(workflow["on"]["pull_request"]["types"]) == {"opened", "synchronize", "reopened"}
    jobs = workflow["jobs"]
    for name in ("visual-contracts", "visual-capture-execution"):
        assert "if" not in jobs[name] and not jobs[name].get("continue-on-error")
    gate = jobs["visual-capture"]
    assert gate["if"] == "${{ always() }}", "the visual gate must report upstream failures"
    assert set(gate["needs"]) == {"visual-contracts", "visual-capture-execution", "visual-capture-serial-control"}
    assert not gate.get("continue-on-error")


@pytest.mark.parametrize("mutation", ["paths", "paths-ignore", "skip-contracts", "ignore-capture", "skip-gate", "omit-control"])
def test_visual_check_cannot_disappear_or_ignore_a_required_failure(mutation: str) -> None:
    workflow = _load()
    _assert_all_changes_have_a_gate(workflow)
    broken = deepcopy(workflow)
    if mutation in {"paths", "paths-ignore"}:
        broken["on"]["pull_request"][mutation] = ["frontend/**"]
    elif mutation == "skip-contracts":
        broken["jobs"]["visual-contracts"]["if"] = "false"
    elif mutation == "ignore-capture":
        broken["jobs"]["visual-capture-execution"]["continue-on-error"] = True
    elif mutation == "skip-gate":
        broken["jobs"]["visual-capture"]["if"] = "success()"
    else:
        broken["jobs"]["visual-capture"]["needs"].remove("visual-capture-serial-control")
    with pytest.raises(AssertionError):
        _assert_all_changes_have_a_gate(broken)


def _assert_contracts_and_safety_are_explicit(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"]["visual-contracts"]
    expected = {
        "Test visual capture contracts": "node --test tools/visual-capture/*.test.mjs",
        "Test restricted browser reporting": "npm run test:ci-tools --prefix tools/e2e-capture",
        "Probe the actual pinned Playwright reporter": "npm run test:ci-sentinels --prefix tools/e2e-capture",
        "Test existing browser diagnostic redaction": "node --test extension/tests/stable-connection-diagnostic.test.mjs extension/tests/worker-recovery-diagnostic.test.mjs",
        "Test Python session diagnostics explicitly": "python -m pytest --override-ini=addopts= tools/e2e-capture/tests/test_session_diagnostics.py",
        "Test visual inventory and output safety": "node --test tools/visual-capture/ci/*.test.mjs",
        "Probe actual Playwright visual output destinations": "node tools/visual-capture/ci/sentinels.mjs",
    }
    for name, command in expected.items():
        candidates = [step for step in job["steps"] if step.get("name") == name]
        assert len(candidates) == 1, "a mandatory visual safety invocation is missing"
        step = candidates[0]
        assert step["run"] == command
        assert "if" not in step and not step.get("continue-on-error")
    surfaces = _step(job, "Test extension surface interactions")
    assert "npm run test:browser:surfaces --prefix extension" in surfaces["run"]
    assert "OFCA_CHROMIUM=" in surfaces["run"]
    assert "if" not in surfaces and not surfaces.get("continue-on-error")
    requirements = (ROOT / "requirements-dev.txt").read_text().splitlines()
    install = _step(job, "Install pinned reporting safety dependencies")["run"]
    for name in ("pytest", "pytest-asyncio"):
        assert next(line for line in requirements if line.startswith(f"{name}==")) in install
    assert "needs" not in workflow["jobs"]["visual-capture-execution"], "capture feedback must start independently"
    assert workflow["jobs"]["visual-capture-serial-control"]["needs"] == "visual-contracts"


@pytest.mark.parametrize("mutation", ["drop-surface", "drop-probe", "drop-visual-probe", "drop-python", "skip-safety", "ignore-safety", "serialize-capture"])
def test_visual_browser_and_privacy_contracts_remain_mandatory(mutation: str) -> None:
    workflow = _load()
    _assert_contracts_and_safety_are_explicit(workflow)
    broken = deepcopy(workflow)
    contracts = broken["jobs"]["visual-contracts"]
    names = {"drop-surface": "Test extension surface interactions", "drop-probe": "Probe the actual pinned Playwright reporter", "drop-visual-probe": "Probe actual Playwright visual output destinations", "drop-python": "Test Python session diagnostics explicitly"}
    if mutation in names:
        contracts["steps"].remove(_step(contracts, names[mutation]))
    elif mutation == "serialize-capture":
        broken["jobs"]["visual-capture-execution"]["needs"] = "visual-contracts"
    else:
        _step(contracts, "Test visual inventory and output safety")["if" if mutation == "skip-safety" else "continue-on-error"] = True
    with pytest.raises((AssertionError, StopIteration)):
        _assert_contracts_and_safety_are_explicit(broken)


def _assert_isolated_groups_and_optional_control(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    capture = jobs["visual-capture-execution"]
    assert capture["name"] == "visual-capture-${{ matrix.group }}"
    assert capture["strategy"] == {"fail-fast": False, "max-parallel": 2, "matrix": {"group": ["dynamic", "remaining"]}}
    for name in PRODUCERS:
        job = jobs[name]
        assert job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] == 20
        assert not job.get("continue-on-error")
        group = "${{ matrix.group }}" if name == "visual-capture-execution" else "all"
        execute = _step(job, "Capture the required visual stage group")
        assert execute["run"] == f"node tools/visual-capture/capture.mjs artifacts/visual-raw/{group} --stage-group {group}"
        assert "if" not in execute and not execute.get("continue-on-error")
        assert set(job["env"]) == {"VISUAL_CI_GROUP"}
        assert job["env"]["VISUAL_CI_GROUP"] == group
        steps = job["steps"]
        assert steps.index(_step(job, "Build extension renderer artifact")) < steps.index(execute)
        for command in ("npm ci --prefix frontend", "npm ci --prefix extension", "npm ci --prefix tools/visual-capture"):
            assert any(step.get("run") == command for step in steps[:steps.index(execute)])
        assert not any(str(step.get("uses", "")).startswith("actions/download-artifact@") for step in steps)
    expected = json.loads(json.dumps(capture).replace("${{ matrix.group }}", "all"))
    del expected["strategy"]
    expected["name"] = "visual-capture-serial-control"
    expected["needs"] = "visual-contracts"
    expected["if"] = "${{ github.event_name == 'workflow_dispatch' && inputs.visual_serial_control }}"
    assert jobs["visual-capture-serial-control"] == expected, "control must execute the same owned bootstrap and full capture"
    for flag in ("visual_qualification", "visual_serial_control"):
        settings = workflow["on"]["workflow_dispatch"]["inputs"][flag]
        assert settings["type"] == "boolean" and settings["default"] is False
        assert workflow["env"][flag.upper()] == "${{ github.event_name == 'workflow_dispatch' && inputs." + flag + " }}"
    guard = _step(jobs["visual-contracts"], "Validate visual qualification request")
    assert guard == {
        "name": "Validate visual qualification request",
        "run": "if [[ \"$VISUAL_SERIAL_CONTROL\" == 'true' && \"$VISUAL_QUALIFICATION\" != 'true' ]]; then\n"
               "  echo '::error::visual_serial_control requires visual_qualification'\n"
               "  exit 1\nfi\n",
    }


@pytest.mark.parametrize("mutation", ["drop-group", "duplicate-group", "unbounded", "shorten-control", "optional-capture", "skip-qualification", "unconditional-control"])
def test_capture_split_preserves_owned_full_populations_and_bounds(mutation: str) -> None:
    workflow = _load()
    _assert_isolated_groups_and_optional_control(workflow)
    broken = deepcopy(workflow)
    jobs = broken["jobs"]
    capture = jobs["visual-capture-execution"]
    if mutation == "drop-group":
        capture["strategy"]["matrix"]["group"] = ["dynamic"]
    elif mutation == "duplicate-group":
        capture["strategy"]["matrix"]["group"] = ["dynamic", "dynamic"]
    elif mutation == "unbounded":
        capture["strategy"]["max-parallel"] = 4
    elif mutation == "shorten-control":
        _step(jobs["visual-capture-serial-control"], "Capture the required visual stage group")["run"] += " --only analytics"
    elif mutation == "optional-capture":
        _step(capture, "Capture the required visual stage group")["continue-on-error"] = True
    elif mutation == "skip-qualification":
        _step(jobs["visual-contracts"], "Validate visual qualification request")["run"] = "true"
    else:
        del jobs["visual-capture-serial-control"]["if"]
    with pytest.raises(AssertionError):
        _assert_isolated_groups_and_optional_control(broken)


def test_visual_capture_caches_only_chromium_downloads_by_exact_lockfile() -> None:
    for name in ("visual-contracts", *PRODUCERS):
        job = _load()["jobs"][name]
        cache = _step(job, "Keep Playwright Chromium in the Actions cache")
        assert cache["with"]["path"] == "~/.cache/ms-playwright"
        key = cache["with"]["key"]
        for required in ("${{ runner.os }}", "${{ runner.arch }}", "tools/visual-capture/package-lock.json"):
            assert required in key
        assert "restore-keys" not in cache["with"]
        install = _step(job, "Install Playwright Chromium")
        assert install["if"] == "steps.playwright-cache.outputs.cache-hit != 'true'"
        assert "if" not in _step(job, "Install Chromium system dependencies")


def _assert_safe_complete_publication(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    for name in PRODUCERS:
        job = jobs[name]
        group = "${{ matrix.group }}" if name == "visual-capture-execution" else "all"
        seal = _step(job, "Seal approved visual producer evidence")
        assert seal["if"] == "always()" and seal["id"] == "visual-seal"
        assert seal["run"] == f"python tools/ci_visual_gate.py seal --artifact-dir artifacts/visual-raw/{group} --publish-dir artifacts/visual-published/{group} --diagnostics-dir artifacts/visual-diagnostics/{group}"
        complete = _step(job, "Retain immutable complete visual inputs")
        assert complete["if"] == "${{ success() && steps.visual-seal.outcome == 'success' }}"
        diagnostic = _step(job, "Retain restricted visual diagnostics")
        assert diagnostic["if"] == "always()"
        for step, prefix, directory in ((complete, "visual-inputs", "visual-published"), (diagnostic, "visual-diagnostics", "visual-diagnostics")):
            assert step["with"]["path"] == f"artifacts/{directory}/{group}/"
            assert step["with"]["name"] == f"{prefix}-{group}-${{{{ env.PRODUCT_SHA }}}}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
            assert step["with"]["retention-days"] == 14
            assert not step["with"].get("overwrite")
        assert job["steps"].index(seal) < job["steps"].index(complete)
    gate = jobs["visual-capture"]
    assert gate["permissions"] == {"contents": "read", "actions": "read"}
    download = _step(gate, "Retrieve immutable complete visual inputs")
    assert download["with"]["pattern"] == "visual-inputs-*"
    assert download["with"]["merge-multiple"] is False
    validate = _step(gate, "Validate and assemble complete visual coverage")
    assert validate["env"] == {"CI_NEEDS_JSON": "${{ toJSON(needs) }}", "GH_TOKEN": "${{ github.token }}"}
    assert validate["run"] == "python tools/ci_visual_gate.py --reports-dir artifacts/visual-evidence --output-dir artifacts/visual-capture --verified-output artifacts/visual-ci-verified.json"
    color = _step(gate, "Record color qualification")
    assert color["run"] == "npm run generate:theme --prefix frontend -- --color-report ../artifacts/visual-capture/color-qualification.json"
    finalize = _step(gate, "Bind the final color report and approved captures")
    assert finalize["run"] == "python tools/ci_visual_gate.py finalize --output-dir artifacts/visual-capture --verified-output artifacts/visual-ci-verified.json"
    proof = _step(gate, "Retain verified visual assembly provenance")
    assert proof["with"]["path"] == "artifacts/visual-ci-verified.json"
    assert proof["with"]["name"] == "visual-verified-${{ env.PRODUCT_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}"
    assert not proof["with"].get("overwrite")
    final = _step(gate, "Upload visual states")
    assert final["with"] == {"name": "visual-states-${{ env.PRODUCT_SHA }}", "path": "artifacts/visual-capture/", "retention-days": 30, "if-no-files-found": "error", "overwrite": True}
    indexes = []
    for step in (validate, color, finalize, proof, final):
        assert "if" not in step and not step.get("continue-on-error"), "complete visual publication requires every acceptance step to pass"
        indexes.append(gate["steps"].index(step))
    assert indexes == sorted(indexes), "color qualification and final hashing must precede publication"


@pytest.mark.parametrize("mutation", ["raw-diagnostics", "unsealed-inputs", "failed-inputs", "overwrite-inputs", "merge-attempts", "skip-validation", "skip-colors", "ignore-finalize", "publish-failure"])
def test_visual_artifacts_refuse_partial_raw_or_unqualified_complete_outputs(mutation: str) -> None:
    workflow = _load()
    _assert_safe_complete_publication(workflow)
    broken = deepcopy(workflow)
    producer = broken["jobs"]["visual-capture-execution"]
    gate = broken["jobs"]["visual-capture"]
    if mutation == "raw-diagnostics":
        _step(producer, "Retain restricted visual diagnostics")["with"]["path"] = "artifacts/visual-raw/"
    elif mutation == "unsealed-inputs":
        _step(producer, "Retain immutable complete visual inputs")["with"]["path"] = "artifacts/visual-raw/"
    elif mutation == "failed-inputs":
        _step(producer, "Retain immutable complete visual inputs")["if"] = "always()"
    elif mutation == "overwrite-inputs":
        _step(producer, "Retain immutable complete visual inputs")["with"]["overwrite"] = True
    elif mutation == "merge-attempts":
        _step(gate, "Retrieve immutable complete visual inputs")["with"]["merge-multiple"] = True
    elif mutation == "skip-validation":
        _step(gate, "Validate and assemble complete visual coverage")["if"] = "false"
    elif mutation == "skip-colors":
        _step(gate, "Record color qualification")["if"] = "false"
    elif mutation == "ignore-finalize":
        _step(gate, "Bind the final color report and approved captures")["continue-on-error"] = True
    else:
        _step(gate, "Upload visual states")["if"] = "always()"
    with pytest.raises(AssertionError):
        _assert_safe_complete_publication(broken)
