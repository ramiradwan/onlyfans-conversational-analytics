"""Render the non-gating, two-ref main-equivalent topology experiment.

Use from the fully stacked candidate source, not on main. No publication or
production gate can be authorized by this workflow. Requires PyYAML.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
REFS = ["bench/windows-topology/qualification-1", "bench/windows-topology/qualification-2"]


def render(ci, visual):
    trial = deepcopy(ci)
    trial["name"] = "Windows topology qualification"
    trial["on"] = {"push": {"branches": REFS}}
    trial["concurrency"] = {"group": "${{ github.workflow }}-${{ github.run_id }}", "cancel-in-progress": False}
    trial["env"].update({k: v for k, v in visual["env"].items() if k != "PRODUCT_SHA"})
    for name in ("BROWSER_QUALIFICATION", "BROWSER_SERIAL_CONTROL", "VISUAL_QUALIFICATION", "VISUAL_SERIAL_CONTROL"):
        trial["env"][name] = "false"
    jobs = trial["jobs"]
    jobs["browser-e2e-serial-control"]["if"] = "${{ false }}"
    wheel = jobs["fixed-sqlcipher-wheel"]
    wheel.pop("permissions", None)
    wheel["steps"] = [step for step in wheel["steps"] if step.get("name") not in {
        "Attest protected main wheel manifest", "Stage attested wheel cache", "Save attested fixed wheel cache", "Save native dependencies on protected main"}]
    backend = jobs["backend-fast"]
    names = {"Check documentation", "Validate architecture boundary manifest", "Check protected architecture impact declaration"}
    preflight = [deepcopy(step) for step in backend["steps"] if step.get("name") in names]
    backend["steps"] = [step for step in backend["steps"] if step.get("name") not in names]
    checkout = deepcopy(backend["steps"][0])
    python = deepcopy(next(step for step in backend["steps"] if step.get("name") == "Setup Python"))
    python["with"] = {"python-version": "3.11"}
    jobs["candidate-preflight"] = {"runs-on": "ubuntu-latest", "timeout-minutes": 5, "steps": [checkout, python, *preflight]}
    platform = jobs["windows-platform-contract"]
    steps = platform["steps"]
    index = {step["name"]: i for i, step in enumerate(steps)}
    prefix = deepcopy(steps[:index["Collect independent legacy Windows selection"]])
    reference = deepcopy(steps[index["Collect independent legacy Windows selection"]:index["Start host counter sampling"]])
    auxiliary = deepcopy(steps[index["Test Windows production boot"]:index["Retain backend timing and selection"]])
    qualifier = deepcopy(platform)
    qualifier.pop("name", None)
    qualifier.pop("strategy")
    qualifier["steps"] = [step for step in prefix if step.get("name") != "Describe Windows test host"]
    qualifier["steps"] += reference + auxiliary
    for step in qualifier["steps"]:
        if step.get("if") == "matrix.shard == 1":
            del step["if"]
        elif step.get("if") == "always() && matrix.shard == 1":
            step["if"] = "always()"
    retain = deepcopy(steps[-1])
    retain["with"]["name"] = "ci-tests-windows-qualification-${{ env.PRODUCT_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}"
    qualifier["steps"].append(retain)
    jobs["windows-qualification"] = qualifier
    platform["steps"] = steps[:index["Collect independent legacy Windows selection"]] + steps[index["Start host counter sampling"]:index["Test Windows production boot"]] + [steps[-1]]
    platform["strategy"]["matrix"]["shard"] = list(range(1, 5))
    platform["strategy"]["max-parallel"] = 4
    full = jobs["windows-full-shards"]
    full["strategy"]["matrix"]["shard"] = list(range(1, 11))
    full["strategy"]["max-parallel"] = 10
    for key, group, old, execute_name in (
        ("windows-platform-contract", "platform-${{ matrix.shard }}", "windows-platform-contract-${{ matrix.shard }}", "Test Windows platform contract"),
        ("windows-full-shards", "full-${{ matrix.shard }}", "windows-full-regression-${{ matrix.shard }}", "Test complete Windows backend regression shard"),
        ("analytics-windows-contract", "analytics", "analytics-windows-contract", "Test Windows analytics contract"),
    ):
        job = jobs[key]
        for step in job["steps"]:
            if step.get("name") == execute_name:
                step["run"] = f"python tools/test_backend.py --lane windows-full-regression -- --ci-windows-candidate {group}"
                for k in ("CI_TEST_LANE", "CI_REPORT_DIR", "PYTEST_ADDOPTS"):
                    step["env"][k] = step["env"][k].replace(old, f"candidate-{group}")
            if step.get("name") == "Stop host counter sampling":
                step["run"] = step["run"].replace(old, f"candidate-{group}")
            if step.get("name") == "Retain backend timing and selection":
                step["with"]["name"] = f"ci-tests-candidate-{group}-" + "${{ env.PRODUCT_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}"
    original_gate = jobs.pop("required-ci-gate")
    jobs.pop("build-and-test")
    jobs.pop("windows-tests")
    gate = deepcopy(original_gate)
    gate["name"] = "Candidate coverage (not release CI)"
    gate["needs"] += ["windows-qualification", "candidate-preflight"]
    gate["steps"][-1] = {"name": "Require current-run candidate coverage and mandatory producers",
        "env": {"CI_NEEDS_JSON": "${{ toJSON(needs) }}"},
        "run": 'python -c \'import json,os; assert all(v["result"] == "success" for v in json.loads(os.environ["CI_NEEDS_JSON"]).values())\'\npython tools/ci_windows_candidate.py --reports artifacts/ci-downloaded'}
    jobs["candidate-coverage"] = gate
    for key, job in visual["jobs"].items():
        if key in jobs:
            raise ValueError("Unexpected job name collision")
        jobs[key] = deepcopy(job)
    jobs["visual-capture-serial-control"]["if"] = "${{ false }}"
    return trial


def main():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    visual = yaml.safe_load((ROOT / ".github/workflows/visual-capture.yml").read_text(encoding="utf-8"))
    destination = ROOT / ".github/workflows/ci-topology-qualification.yml"
    if destination.exists():
        raise SystemExit("Refusing to overwrite an existing trial workflow")
    destination.write_text(yaml.safe_dump(render(ci, visual), sort_keys=False, width=110), encoding="utf-8")


if __name__ == "__main__":
    main()
