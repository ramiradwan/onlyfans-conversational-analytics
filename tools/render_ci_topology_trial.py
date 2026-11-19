"""Render the non-gating, two-ref main-equivalent topology experiment.

Use from the fully stacked candidate source, not on main. No publication or
production gate can be authorized by this workflow. Requires PyYAML.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import sys
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.ci_windows_shards import candidate_counts

REFS = ["bench/windows-topology/qualification-1", "bench/windows-topology/qualification-2"]


def render(ci, visual, *, plan=None, reuse_frontend=False):
    counts = candidate_counts(plan) if plan is not None else {"full": 10, "platform": 4, "analytics": 1}
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
    platform["strategy"]["matrix"]["shard"] = list(range(1, counts["platform"] + 1))
    platform["strategy"]["max-parallel"] = counts["platform"]
    full = jobs["windows-full-shards"]
    full["strategy"]["matrix"]["shard"] = list(range(1, counts["full"] + 1))
    full["strategy"]["max-parallel"] = counts["full"]
    analytics_group = "analytics"
    if plan is not None and plan["schema"] == 2:
        analytics = jobs["analytics-windows-contract"]
        analytics["name"] = "analytics-windows-contract-${{ matrix.shard }}"
        analytics["strategy"] = {"fail-fast": False, "max-parallel": counts["analytics"],
                                 "matrix": {"shard": list(range(1, counts["analytics"] + 1))}}
        analytics_group = "analytics-${{ matrix.shard }}"
    for key, group, old, execute_name in (
        ("windows-platform-contract", "platform-${{ matrix.shard }}", "windows-platform-contract-${{ matrix.shard }}", "Test Windows platform contract"),
        ("windows-full-shards", "full-${{ matrix.shard }}", "windows-full-regression-${{ matrix.shard }}", "Test complete Windows backend regression shard"),
        ("analytics-windows-contract", analytics_group, "analytics-windows-contract", "Test Windows analytics contract"),
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
    gate["needs"] += ["windows-qualification", "candidate-preflight", "visual-capture"]
    gate["steps"][-1] = {"name": "Require current-run candidate coverage and mandatory producers",
        "env": {"CI_NEEDS_JSON": "${{ toJSON(needs) }}"},
        "run": 'python -c \'import json,os; assert all(v["result"] == "success" for v in json.loads(os.environ["CI_NEEDS_JSON"]).values())\'\npython tools/ci_windows_candidate.py --reports artifacts/ci-downloaded'}
    jobs["candidate-coverage"] = gate
    for key, job in visual["jobs"].items():
        if key in jobs:
            raise ValueError("Unexpected job name collision")
        jobs[key] = deepcopy(job)
    jobs["visual-capture-serial-control"]["if"] = "${{ false }}"
    if reuse_frontend:
        attach_frontend_reuse(trial)
    return trial


def attach_frontend_reuse(trial):
    """Opt-in experiment only; preserve the normal web tests and cold builds."""
    jobs = trial["jobs"]
    web = jobs["web-build-and-test"]
    steps = web["steps"]
    checkout = deepcopy(next(s for s in steps if s.get("name") == "Checkout exact Product revision"))
    node = deepcopy(next(s for s in steps if s.get("name") == "Setup Node.js"))
    node["with"]["cache-dependency-path"] = "frontend/package-lock.json"
    install = deepcopy(next(s for s in steps if s.get("name") == "Install frontend dependencies"))
    build = deepcopy(next(s for s in steps if s.get("name") == "Build frontend"))
    build["run"] = "npm run build:assets --prefix frontend"
    verify = deepcopy(next(s for s in steps if s.get("name") == "Check committed theme artifacts against the generator"))
    python = deepcopy(next(s for s in jobs["candidate-preflight"]["steps"] if s.get("name") == "Setup Python"))
    upload = deepcopy(next(s for s in steps if s.get("name") == "Upload frontend build"))
    upload["name"] = "Retain source-bound frontend assets"
    upload["with"]["name"] = "ci-frontend-assets-${{ env.PRODUCT_SHA }}"
    upload["with"]["path"] = "${{ runner.temp }}/ofca-frontend-assets/"
    jobs["web-build-assets"] = {"runs-on": "ubuntu-latest", "timeout-minutes": 10,
        "steps": [checkout, node, install, build, verify, python,
            {"name": "Seal frontend asset receipt", "run": 'python tools/ci_frontend_assets.py seal app/static/dist "$RUNNER_TEMP/ofca-frontend-assets" --producer-attempt "$GITHUB_RUN_ATTEMPT"'}, upload]}
    for key in ("windows-platform-contract", "windows-full-shards", "analytics-windows-contract",
                "analytics-scale-qualification", "windows-qualification"):
        job = jobs[key]
        deps = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        job["needs"] = [*deps, "web-build-assets"]
        job["permissions"] = {"contents": "read", "actions": "read"}
        old_if = job.get("if", "${{ true }}").removeprefix("${{").removesuffix("}}").strip()
        # A failed asset producer does not block the original local build.
        # All original prerequisites must still have succeeded.
        predicate = " && ".join(f"needs.{dep}.result == 'success'" for dep in deps)
        job["if"] = "${{ always() && !cancelled() && (" + old_if + ") && " + predicate + " }}"
        steps = job["steps"]
        index = next(i for i, s in enumerate(steps) if s.get("name") == "Install frontend dependencies")
        fetch = {"name": "Retrieve exact-run frontend assets", "id": "frontend-download",
            "if": "needs.web-build-assets.result == 'success'", "continue-on-error": True,
            "uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "with": {"name": "ci-frontend-assets-${{ env.PRODUCT_SHA }}", "path": "${{ runner.temp }}/ofca-frontend-assets"}}
        restore = {"name": "Verify exact producer and frontend bytes", "id": "frontend-reuse",
            "if": "steps.frontend-download.outcome == 'success'", "shell": "pwsh", "env": {"GH_TOKEN": "${{ github.token }}"},
            "run": 'python tools/ci_frontend_assets.py restore "$env:RUNNER_TEMP/ofca-frontend-assets" app/static/dist --resolve-producer\n'
                   'if ($LASTEXITCODE -eq 0) { "reused=true" >> $env:GITHUB_OUTPUT } else { Write-Warning "Frontend asset verification refused; building locally." }\nexit 0'}
        steps[index:index] = [fetch, restore]
        for step in steps:
            if step.get("name") in {"Install frontend dependencies", "Build frontend"}:
                step["if"] = "steps.frontend-reuse.outputs.reused != 'true'"
    # Include the producer in the experiment's result. Its failure is visible
    # even if every consumer successfully falls back to a local build.
    jobs["candidate-coverage"]["needs"].append("web-build-assets")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-frontend", action="store_true")
    args = parser.parse_args()
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    visual = yaml.safe_load((ROOT / ".github/workflows/visual-capture.yml").read_text(encoding="utf-8"))
    destination = ROOT / ".github/workflows/ci-topology-qualification.yml"
    if destination.exists():
        raise SystemExit("Refusing to overwrite an existing trial workflow")
    plan = json.loads((ROOT / "tools/windows-topology-candidate.json").read_text(encoding="utf-8"))
    destination.write_text(yaml.safe_dump(render(ci, visual, plan=plan, reuse_frontend=args.reuse_frontend), sort_keys=False, width=110), encoding="utf-8")


if __name__ == "__main__":
    main()
