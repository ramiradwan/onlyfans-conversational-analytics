from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.ci_tier("fast")]


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CORE_SOURCE_JOBS = (
    "desktop-native-contracts",
    "web-build-and-test",
    "backend-fast",
    "analytics-integration",
    "fixed-sqlcipher-wheel",
    "windows-platform-contract",
    "analytics-windows-contract",
    "analytics-scale-qualification",
    "windows-full-shards",
    "browser-reporting-safety",
    "browser-e2e-execution",
    "browser-e2e-serial-control",
    "windows-browser-e2e",
    "required-ci-gate", "candidate-preflight", "windows-qualification", "web-build-assets",
)


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_core_ci_uses_one_canonical_product_source_coordinate() -> None:
    workflow = _workflow_document()
    assert workflow["env"]["PRODUCT_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    for name in CORE_SOURCE_JOBS:
        checkout = next(
            step for step in _steps(workflow["jobs"][name])
            if step.get("name") == "Checkout exact Product revision"
        )
        assert checkout["with"]["ref"] == "${{ env.PRODUCT_SHA }}"


def test_transient_build_artifacts_are_source_bound_and_short_lived() -> None:
    steps = _steps(_workflow_document()["jobs"]["web-build-and-test"])
    expected = {
        "Upload frontend build": "frontend-dist-${{ env.PRODUCT_SHA }}",
        "Upload audited extension artifact": "extension-dist-${{ env.PRODUCT_SHA }}",
    }
    for step_name, artifact_name in expected.items():
        step = next(candidate for candidate in steps if candidate.get("name") == step_name)
        assert step["with"]["name"] == artifact_name
        assert step["with"]["retention-days"] == 7
        assert step["with"]["if-no-files-found"] == "error"


def test_sqlcipher_and_persistence_artifacts_record_source_and_run_identity() -> None:
    workflow = _workflow_document()
    sql_steps = _steps(workflow["jobs"]["fixed-sqlcipher-wheel"])
    source = next(step for step in sql_steps if step.get("name") == "Record fixed SQLCipher CI source")
    assert "source_commit = $env:PRODUCT_SHA" in source["run"]
    assert "workflow_run_id" in source["run"]
    sql_upload = next(step for step in sql_steps if step.get("name") == "Retain fixed SQLCipher wheel and provenance")
    assert sql_upload["with"]["name"] == "fixed-sqlcipher-wheel-${{ env.PRODUCT_SHA }}"

    windows_steps = _steps(workflow["jobs"]["windows-platform-contract"])
    verify = next(step for step in windows_steps if step.get("name") == "Verify fixed SQLCipher CI source")
    assert "source SHA mismatch" in verify["run"]
    assert "run ID mismatch" in verify["run"]
    persistence = next(step for step in _steps(workflow["jobs"]["windows-qualification"]) if step.get("name") == "Record Windows persistence CI source")
    assert "source_commit = $env:PRODUCT_SHA" in persistence["run"]
    assert "workflow_run_id" in persistence["run"]


def test_stable_consumer_artifacts_can_be_republished_on_an_exact_source_rerun() -> None:
    expected = {
        "frontend-dist-${{ env.PRODUCT_SHA }}",
        "ci-frontend-assets-${{ env.PRODUCT_SHA }}",
        "extension-dist-${{ env.PRODUCT_SHA }}",
        "fixed-sqlcipher-wheel-${{ env.PRODUCT_SHA }}",
        "windows-persistence-evidence-${{ env.PRODUCT_SHA }}",
        "legal-activation-v2-${{ env.PRODUCT_SHA }}",
    }
    observed = set()
    for job in _workflow_document()["jobs"].values():
        for step in _steps(job):
            if not str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                continue
            settings = step["with"]
            if settings["name"] in expected:
                assert settings["overwrite"] is True
                observed.add(settings["name"])
            else:
                assert settings["name"].startswith(("ci-tests-", "browser-e2e-inputs-", "browser-e2e-diagnostics-", "browser-e2e-verified-", "desktop-native-contracts-"))
                assert not settings.get("overwrite"), "retained attempt evidence cannot be replaced"
    assert observed == expected


def test_native_desktop_outcomes_remain_immutable_and_attempt_specific() -> None:
    steps = _steps(_workflow_document()["jobs"]["desktop-native-contracts"])
    retain = next(step for step in steps if step.get("name") == "Retain native contract outcomes")
    assert retain["if"] == "always()"
    assert retain["with"]["name"] == "desktop-native-contracts-${{ env.PRODUCT_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}"
    assert retain["with"]["if-no-files-found"] == "error"
    assert not retain["with"].get("overwrite")


def test_windows_platform_evidence_keeps_each_shard_and_auxiliary_producer_distinct() -> None:
    job = _workflow_document()["jobs"]["windows-platform-contract"]
    assert job["name"] == "windows-platform-contract-${{ matrix.shard }}"
    assert job["strategy"]["matrix"] == {"shard": [1, 2, 3, 4, 5]}
    assert job["strategy"]["fail-fast"] is False
    steps = _steps(job)
    execute = next(step for step in steps if step.get("name") == "Test Windows platform contract")
    assert execute["run"] == "python tools/test_backend.py --lane windows-full-regression -- --ci-windows-candidate platform-${{ matrix.shard }}"
    assert execute["env"]["CI_TEST_LANE"] == "candidate-platform-${{ matrix.shard }}"
    assert execute["env"]["CI_REPORT_DIR"] == "artifacts/ci-tests/candidate-platform-${{ matrix.shard }}"
    retain = next(step for step in steps if step.get("name") == "Retain backend timing and selection")
    assert retain["if"] == "always()"
    assert retain["with"]["name"] == "ci-tests-candidate-platform-${{ matrix.shard }}-${{ env.PRODUCT_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}"
    assert retain["with"]["if-no-files-found"] == "error"
    assert not retain["with"].get("overwrite")
    auxiliary_lanes = {
        "legacy-windows-reference", "windows-production-boot", "windows-python313-loop-contract",
        "windows-persistence-general", "windows-persistence-deletion", "windows-persistence-smoke",
    }
    observed = set()
    assert not any(step.get("env", {}).get("CI_TEST_LANE") in auxiliary_lanes for step in steps)
    steps = _steps(_workflow_document()["jobs"]["windows-qualification"])
    for step in steps:
        lane = step.get("env", {}).get("CI_TEST_LANE")
        if lane in auxiliary_lanes:
            assert "if" not in step
            observed.add(lane)
    assert observed == auxiliary_lanes
    for name in ("Preserve persistence evidence filenames", "Record Windows persistence CI source",
                 "Retain Windows persistence evidence"):
        assert next(step for step in steps if step.get("name") == name)["if"] == "always()"


def _assert_browser_publication_is_validated(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    producer = jobs["browser-e2e-execution"]
    steps = _steps(producer)
    execute = next(step for step in steps if step.get("id") == "browser-execution")
    seal = next(step for step in steps if step.get("id") == "browser-seal")
    complete = next(step for step in steps if step.get("name") == "Retain immutable browser producer evidence")
    diagnostic = next(step for step in steps if step.get("name") == "Retain restricted browser diagnostics")
    assert steps.index(execute) < steps.index(seal) < steps.index(complete)
    assert seal["if"] == "always()"
    assert seal["run"] == (
        'python tools/ci_browser_gate.py seal --artifact-dir "$env:BROWSER_CI_REPORT_DIR" '
        '--publish-dir "$env:BROWSER_CI_REPORT_DIR-published" '
        '--diagnostics-dir "$env:BROWSER_CI_REPORT_DIR-diagnostics"'
    )
    assert complete["if"] == "${{ success() && steps.browser-seal.outcome == 'success' }}"
    assert complete["with"]["path"] == "${{ env.BROWSER_CI_REPORT_DIR }}-published/"
    assert diagnostic["if"] == "always()"
    assert diagnostic["with"]["path"] == "${{ env.BROWSER_CI_REPORT_DIR }}-diagnostics/"
    for step, prefix in ((complete, "browser-e2e-inputs"), (diagnostic, "browser-e2e-diagnostics")):
        assert step["with"]["name"] == f"{prefix}-${{{{ matrix.lane }}}}-${{{{ env.PRODUCT_SHA }}}}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
        assert not step["with"].get("overwrite"), "producer evidence must remain immutable"
        assert step["with"]["retention-days"] == 14
    aggregate = jobs["windows-browser-e2e"]
    assert aggregate["permissions"] == {"contents": "read", "actions": "read"}
    steps = _steps(aggregate)
    retrieve = next(step for step in steps if step.get("name") == "Retrieve immutable browser producer evidence")
    assert retrieve["with"]["pattern"] == "browser-e2e-inputs-*"
    assert retrieve["with"]["merge-multiple"] is False
    validate = next(step for step in steps if step.get("name") == "Validate browser execution and assemble Legal inputs")
    assert validate["run"] == (
        "python tools/ci_browser_gate.py --reports-dir artifacts/browser-evidence "
        "--execution-root artifacts/legal/execution --runtime-proof artifacts/legal/runtime-ui-proof.json "
        "--verified-output artifacts/browser-evidence/assembly-receipt.json"
    )
    assert validate["env"] == {"CI_NEEDS_JSON": "${{ toJSON(needs) }}", "GH_TOKEN": "${{ github.token }}"}
    generate = next(step for step in steps if step.get("name") == "Generate Product #5 Legal evidence bundle")
    publish = next(step for step in steps if step.get("name") == "Upload Product #5 Legal evidence bundle")
    assert steps.index(retrieve) < steps.index(validate) < steps.index(generate) < steps.index(publish)
    for step in (validate, generate, publish):
        assert "if" not in step and not step.get("continue-on-error"), "Legal publication requires successful validation"
    assert publish["with"]["name"] == "legal-activation-v2-${{ env.PRODUCT_SHA }}"
    assert publish["with"]["overwrite"] is True


@pytest.mark.parametrize("mutation", ["raw-diagnostics", "unsealed-inputs", "failed-inputs", "overwrite-producer", "merged-attempts", "skip-validator", "publish-after-failure"])
def test_browser_artifacts_cannot_publish_unvalidated_or_failed_raw_evidence(mutation: str) -> None:
    workflow = _workflow_document()
    _assert_browser_publication_is_validated(workflow)
    broken = deepcopy(workflow)
    producer = _steps(broken["jobs"]["browser-e2e-execution"])
    aggregate = _steps(broken["jobs"]["windows-browser-e2e"])
    if mutation == "raw-diagnostics":
        next(step for step in producer if step.get("name") == "Retain restricted browser diagnostics")["with"]["path"] = "${{ env.BROWSER_CI_REPORT_DIR }}/"
    elif mutation == "unsealed-inputs":
        next(step for step in producer if step.get("name") == "Retain immutable browser producer evidence")["with"]["path"] = "${{ env.BROWSER_CI_REPORT_DIR }}/"
    elif mutation == "failed-inputs":
        next(step for step in producer if step.get("name") == "Retain immutable browser producer evidence")["if"] = "always()"
    elif mutation == "overwrite-producer":
        next(step for step in producer if step.get("name") == "Retain immutable browser producer evidence")["with"]["overwrite"] = True
    elif mutation == "merged-attempts":
        next(step for step in aggregate if step.get("name") == "Retrieve immutable browser producer evidence")["with"]["merge-multiple"] = True
    elif mutation == "skip-validator":
        next(step for step in aggregate if step.get("name") == "Validate browser execution and assemble Legal inputs")["if"] = "false"
    else:
        next(step for step in aggregate if step.get("name") == "Upload Product #5 Legal evidence bundle")["if"] = "always()"
    with pytest.raises(AssertionError):
        _assert_browser_publication_is_validated(broken)



def frontend_asset_tree(tmp_path):
    from tools.ci_frontend_assets import REQUIRED
    root = tmp_path / "dist"
    for name in REQUIRED | {"assets/bundle-X.js", "assets/style.css"}:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    return root


def test_frontend_assets_exact_source_and_attempt_roundtrip(tmp_path):
    from tools.ci_frontend_assets import seal, restore, compare
    source = frontend_asset_tree(tmp_path)
    bundle = tmp_path / "bundle"
    target = tmp_path / "consumer" / "dist"
    args = dict(source="a"*40, run=12, attempt=2)
    receipt = seal(source, bundle, **args)
    assert restore(bundle, target, **args) == receipt
    assert compare(source, target)["equal"] is True


@pytest.mark.parametrize("fault", ["bytes", "extra", "missing", "source", "run", "attempt", "receipt-extra", "existing-target"])
def test_frontend_asset_restore_refuses_ambiguous_or_tampered_inputs(tmp_path, fault):
    import json
    from tools.ci_frontend_assets import seal, restore, AssetError
    source = frontend_asset_tree(tmp_path)
    bundle = tmp_path / "bundle"
    target = tmp_path / "consumer"
    args = dict(source="a"*40, run=12, attempt=2)
    seal(source, bundle, **args)
    if fault == "bytes": (bundle / "dist/index.html").write_text("modified")
    elif fault == "extra": (bundle / "dist/extra.js").write_text("modified")
    elif fault == "missing": (bundle / "dist/assets/style.css").unlink()
    elif fault == "source": args["source"] = "b"*40
    elif fault == "run": args["run"] = 13
    elif fault == "attempt": args["attempt"] = 3
    elif fault == "receipt-extra":
        path = bundle / "receipt.json"
        receipt = json.loads(path.read_text())
        receipt["extra"] = True
        path.write_text(json.dumps(receipt))
    elif fault == "existing-target": target.mkdir()
    with pytest.raises(AssetError):
        restore(bundle, target, **args)
    if fault != "existing-target": assert not target.exists()


def test_frontend_asset_inventory_rejects_hidden_and_unsafe_names(tmp_path):
    from tools.ci_frontend_assets import inventory, AssetError
    root = frontend_asset_tree(tmp_path)
    (root / ".hidden").write_text("hidden")
    with pytest.raises(AssetError, match="unsafe asset path"):
        inventory(root)


def test_frontend_asset_comparison_reports_exact_differences(tmp_path):
    from tools.ci_frontend_assets import compare
    import shutil
    root = frontend_asset_tree(tmp_path)
    other = tmp_path / "other"
    shutil.copytree(root, other)
    (other / "assets/style.css").write_text("changed")
    (other / "extra.js").write_text("extra")
    result = compare(root, other)
    assert result["equal"] is False
    assert result["changed"] == ["assets/style.css"] and result["only_second"] == ["extra.js"]


def test_frontend_asset_producer_attempt_is_authoritative(monkeypatch):
    from tools import engineering_attestation
    from tools.ci_frontend_assets import successful_producer_attempt, AssetError
    coordinate = dict(source="a"*40, run=12, current_attempt=3)
    def jobs(client, **kwargs):
        assert kwargs == dict(run_id=12, run_attempt=3, source_commit="a"*40)
        return {"web-build-assets": {"status": "completed", "conclusion": "success", "run_attempt": 1}}
    monkeypatch.setattr(engineering_attestation, "latest_ci_jobs", jobs)
    assert successful_producer_attempt(None, **coordinate) == 1
    monkeypatch.setattr(engineering_attestation, "latest_ci_jobs", lambda *a, **k: {"web-build-assets": {"status": "completed", "conclusion": "failure", "run_attempt": 3}})
    with pytest.raises(AssetError):
        successful_producer_attempt(None, **coordinate)


def test_trial_frontend_reuse_preserves_checks_and_cold_fallback():
    from tools.render_ci_topology_trial import render
    ci = yaml.safe_load((ROOT / "tests/fixtures/product-ci-sharded-v8-pr-cutover.yml").read_text(encoding="utf-8"))
    visual = yaml.safe_load((ROOT / ".github/workflows/visual-capture.yml").read_text(encoding="utf-8"))
    baseline = render(ci, visual)
    candidate = render(ci, visual, reuse_frontend=True)
    assert "web-build-assets" not in baseline["jobs"]
    assert "web-build-assets" not in ci["jobs"]
    assert candidate["jobs"]["web-build-and-test"] == baseline["jobs"]["web-build-and-test"]
    assert "web-build-assets" in candidate["jobs"]["candidate-coverage"]["needs"]
    for key in ("windows-platform-contract", "windows-full-shards", "analytics-windows-contract", "analytics-scale-qualification", "windows-qualification"):
        job = candidate["jobs"][key]
        assert "web-build-assets" in job["needs"]
        assert "always()" in job["if"] and "!cancelled()" in job["if"]
        assert "needs.fixed-sqlcipher-wheel.result == 'success'" in job["if"]
        steps = {s["name"]: s for s in job["steps"]}
        assert steps["Retrieve exact-run frontend assets"]["continue-on-error"] is True
        assert "--resolve-producer" in steps["Verify exact producer and frontend bytes"]["run"]
        for name in ("Install frontend dependencies", "Build frontend"):
            assert steps[name]["if"] == "steps.frontend-reuse.outputs.reused != 'true'"
        for name in ("Install extension dependencies", "Build extension"):
            if name in steps:
                assert steps[name] == next(s for s in baseline["jobs"][key]["steps"] if s["name"] == name)


FRONTEND_REUSE_CONSUMERS = ("windows-platform-contract", "windows-full-shards", "analytics-windows-contract",
                            "analytics-scale-qualification", "windows-qualification")


def _assert_active_frontend_reuse(workflow):
    jobs = workflow["jobs"]
    producer = jobs["web-build-assets"]
    assert "if" not in producer and not producer.get("continue-on-error")
    assert "web-build-assets" in jobs["required-ci-gate"]["needs"]
    steps = _steps(producer)
    by_name = {s["name"]: s for s in steps}
    assert by_name["Install frontend dependencies"]["run"] == "npm ci --prefix frontend"
    assert by_name["Build frontend"]["run"] == "npm run build:assets --prefix frontend"
    assert by_name["Seal frontend asset receipt"]["run"] == 'python tools/ci_frontend_assets.py seal app/static/dist "$RUNNER_TEMP/ofca-frontend-assets" --producer-attempt "$GITHUB_RUN_ATTEMPT"'
    upload = by_name["Retain source-bound frontend assets"]
    assert upload["with"]["name"] == "ci-frontend-assets-${{ env.PRODUCT_SHA }}"
    assert steps.index(by_name["Build frontend"]) < steps.index(by_name["Seal frontend asset receipt"]) < steps.index(upload)
    for name in FRONTEND_REUSE_CONSUMERS:
        job = jobs[name]
        assert set(job["needs"]) == {"fixed-sqlcipher-wheel", "web-build-assets"}
        assert job["permissions"] == {"contents": "read", "actions": "read"}
        steps = _steps(job)
        by_name = {s["name"]: s for s in steps}
        fetch, verify = by_name["Retrieve exact-run frontend assets"], by_name["Verify exact producer and frontend bytes"]
        assert fetch["id"] == "frontend-download" and fetch["continue-on-error"] is True
        assert fetch["if"] == "needs.web-build-assets.result == 'success'"
        assert fetch["with"]["name"] == upload["with"]["name"]
        assert verify["id"] == "frontend-reuse" and verify["if"] == "steps.frontend-download.outcome == 'success'"
        assert verify["env"] == {"GH_TOKEN": "${{ github.token }}"}
        assert verify["run"] == ('python tools/ci_frontend_assets.py restore "$env:RUNNER_TEMP/ofca-frontend-assets" app/static/dist --resolve-producer\n'
                                 'if ($LASTEXITCODE -eq 0) { "reused=true" >> $env:GITHUB_OUTPUT } else { Write-Warning "Frontend asset verification refused; building locally." }\nexit 0')
        install, build = by_name["Install frontend dependencies"], by_name["Build frontend"]
        assert steps.index(fetch) < steps.index(verify) < steps.index(install) < steps.index(build)
        for step in (install, build):
            assert step["if"] == "steps.frontend-reuse.outputs.reused != 'true'" and not step.get("continue-on-error")
        for step in steps:
            if step.get("name") in {"Install extension dependencies", "Build extension"}:
                assert "if" not in step and not step.get("continue-on-error")


def test_active_frontend_reuse_preserves_verified_fallback():
    _assert_active_frontend_reuse(_workflow_document())


@pytest.mark.parametrize("fault", ["missing-verifier", "unverified-output", "wrong-attempt", "missing-fallback", "missing-producer", "optional-extension"])
def test_active_frontend_reuse_refuses_lost_provenance_or_fallback(fault):
    workflow = deepcopy(_workflow_document())
    job = workflow["jobs"]["windows-platform-contract"]
    steps = job["steps"]
    verify = next(s for s in steps if s.get("id") == "frontend-reuse")
    if fault == "missing-verifier": steps.remove(verify)
    elif fault == "unverified-output": verify["run"] = '"reused=true" >> $env:GITHUB_OUTPUT'
    elif fault == "wrong-attempt": verify["run"] = verify["run"].replace("--resolve-producer", "--producer-attempt 1")
    elif fault == "missing-fallback": next(s for s in steps if s.get("name") == "Build frontend")["if"] = "false"
    elif fault == "missing-producer": workflow["jobs"]["required-ci-gate"]["needs"].remove("web-build-assets")
    else: next(s for s in steps if s.get("name") == "Build extension")["if"] = "false"
    with pytest.raises((AssertionError, KeyError)):
        _assert_active_frontend_reuse(workflow)
