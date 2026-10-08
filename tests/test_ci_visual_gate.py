"""Independent visual evidence, publication and rerun refusal tests."""
from __future__ import annotations

import copy
import json
import struct
from pathlib import Path

import pytest

from tools import ci_visual_gate as gate

pytestmark = pytest.mark.ci_tier("fast")
SOURCE = "a" * 40
NOW = "2026-06-30T12:05:00.000Z"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def inventory():
    cases = [
        {"id": "dynamic-home-390-light-1-reduce", "kind": "dynamic", "group": "dynamic",
         "configuration": {"view": "home", "width": 390, "mode": "light", "fontScale": 1, "motion": "reduce"},
         "observations": ["loading", "snapshot:fresh", "loading"],
         "report": "transitions/home.json", "files": ["transitions/home.json"]},
        {"id": "ordinary-home-fresh-light-desktop", "kind": "ordinary", "group": "remaining",
         "configuration": {"workspace": "home", "state": "fresh", "variant": None, "mode": "light",
                           "viewport": "desktop", "width": 1440, "height": 900},
         "observations": ["fold", "full", "vision:protanopia", "vision:deuteranopia"], "report": "home-fresh-light-desktop-geometry.json",
         "files": ["home-fresh-light-desktop-geometry.json", "home/home-fresh-light-desktop-fold.png", "home/home-fresh-light-desktop-full.png",
                   "diagnostics/vision/home-fresh-light-desktop-protanopia.png", "diagnostics/vision/home-fresh-light-desktop-deuteranopia.png"]},
        {"id": "freshness-390-light-1-reduce", "kind": "freshness", "group": "remaining",
         "configuration": {"width": 390, "mode": "light", "fontScale": 1, "motion": "reduce"},
         "observations": ["edge:freshness-0:freshness-1", "edge:freshness-1:freshness-0", "overlay:freshness-0", "overlay:freshness-1"],
         "report": "freshness.json", "files": ["freshness.json"]},
        {"id": "review-light-rail", "kind": "review", "group": "remaining",
         "configuration": {"mode": "light", "name": "light: rail"}, "observations": ["light: rail"],
         "report": "review/acceptance.json", "files": ["review/acceptance.json", "review/rail-light.png"]},
        {"id": "static-popup-ready-light-390", "kind": "static", "group": "remaining",
         "configuration": {"surface": "popup", "state": "ready", "mode": "light", "viewport": {"width": 390, "height": 600}},
         "observations": ["check:popup-ready-light-390", "fold", "full"], "report": "static-surfaces/acceptance.json",
         "files": ["static-surfaces/acceptance.json", "static-surfaces/popup-ready-light-390-geometry.json",
                   "static-surfaces/popup-ready-light-390-fold.png", "static-surfaces/popup-ready-light-390-full.png"]},
    ]
    value = {"schema": "visual-ci-inventory/v1", "workers": 4, "fixed_now": NOW, "cases": cases,
        "shared_files": {"dynamic": ["manifest.json", "transitions/manifest.json"],
                         "remaining": ["manifest.json", "freshness-transitions.json"]}}
    return gate.validate_inventory((json.dumps(value) + "\n").encode())


def _watcher():
    return {"failures": [], "samples": 1, "frames": [{"at": 1, "regions": [{"id": "fixture"}], "errors": []}]}


def _sample(**kwargs):
    return {"before": {"at": 1, "boxes": []}, "after": {"at": 2, "boxes": []}, **kwargs}


def _make(root, inventory_pair, group, *, attempt=1):
    inventory, digest = inventory_pair
    path = root / f"visual-inputs-{group}-{SOURCE}-42-{attempt}"
    path.mkdir(parents=True)
    manifest = {"revision": SOURCE, "fixedNow": NOW, "phaseTimings": {"prepare": 1, "cleanup": 0.1},
        "entries": [], "diagnostics": [], "dynamic": [], "freshness": [], "review": None, "static": None, "failures": []}
    records = []
    for case in gate.selected_cases(inventory, group).values():
        config = case["configuration"]
        records.append({"id": case["id"], "configuration": config, "observations": case["observations"],
                        "files": case["files"], "outcome": "passed", "duration_ms": 100})
        if case["kind"] == "dynamic":
            raw = {"revision": SOURCE, **_watcher(), **{key: value for key, value in config.items() if key != "width"},
                   "viewport": {"width": config["width"], "height": 844},
                   "transitions": [_sample(id=label) for label in case["observations"]]}
            manifest["dynamic"].append({"view": config["view"], "width": config["width"], "file": case["report"],
                "transitions": len(case["observations"]), "states": case["observations"], "failures": []})
        elif case["kind"] == "freshness":
            transitions = [_sample(**dict(zip(("from", "to"), label.split(":")[1:]))) for label in case["observations"] if label.startswith("edge:")]
            overlays = [_sample(state=label.removeprefix("overlay:")) for label in case["observations"] if label.startswith("overlay:")]
            raw = {"revision": SOURCE, **_watcher(), **{key: value for key, value in config.items() if key != "width"},
                   "view": "freshness", "viewport": {"width": config["width"], "height": 844}, "expected": len(transitions),
                   "transitions": transitions, "overlays": overlays}
            manifest["freshness"].append({"file": case["report"], "transitions": len(transitions), "failures": []})
        elif case["kind"] == "ordinary":
            raw = {"revision": SOURCE, **_watcher(), "view": config["workspace"], "transition": "boot:" + config["state"],
                   "mode": config["mode"], "viewport": {"name": config["viewport"], "width": config["width"], "height": config["height"]}}
            for image in [file for file in case["files"] if file.endswith(".png")]:
                if image.startswith("diagnostics/vision/"):
                    manifest["diagnostics"].append({"file": image, "type": image.rsplit("-", 1)[1].removesuffix(".png"),
                        "diagnosticOnly": True, "mode": config["mode"], "viewport": config["viewport"]})
                else:
                    manifest["entries"].append({"file": image, **config, "capture": "fold" if image.endswith("-fold.png") else "full"})
        elif case["kind"] == "review":
            raw = {"revision": SOURCE, "failures": [], "checks": [{"name": config["name"], "result": "passed", "measurements": {}}]}
            manifest["review"] = {"file": "review/acceptance.json", "passed": 1, "failures": 0}
        else:
            name = case["observations"][0].removeprefix("check:")
            geometry = {"revision": SOURCE, **_watcher(), "view": config["surface"], "transition": "boot:" + config["state"],
                        "mode": config["mode"], "viewport": config["viewport"]}
            _write(path / f"static-surfaces/{name}-geometry.json", geometry)
            raw = {"revision": SOURCE, "failures": [], "checks": [{"name": name, "geometry": f"{name}-geometry.json"}],
                   "entries": [{"file": f"static-surfaces/{name}-{capture}.png", **config, "capture": capture} for capture in ("fold", "full")]}
            manifest["static"] = {"file": "static-surfaces/acceptance.json", "passed": 1, "screenshots": 2, "failures": 0}
        _write(path / case["report"], raw)
        for filename in case["files"]:
            if filename.endswith(".png"):
                target = path / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1, 1) + b"\x08\x06\x00\x00\x00" + b"\x00" * 4)
    _write(path / "manifest.json", manifest)
    if group in {"dynamic", "all"}:
        _write(path / "transitions/manifest.json", manifest["dynamic"])
    if group in {"remaining", "all"}:
        _write(path / "freshness-transitions.json", manifest["freshness"])
    receipt = {"schema": "visual-ci-receipt/v1", "source_commit": SOURCE, "workflow_run_id": "42", "run_attempt": attempt,
        "logical_job": gate.JOBS[group], "group": group, "platform": "Linux", "workers": 4, "inventory_sha256": digest,
        "complete": True, "exit_code": 0, "retries": 0, "skips": 0, "cases": records,
        "file_sha256": {file: gate.sha((path / file).read_bytes()) for file in gate.expected_files(inventory, group)},
        "phase_timings": manifest["phaseTimings"], "started_at": "2026-10-03T12:00:00.000Z",
        "finished_at": "2026-10-03T12:01:00.000Z", "duration_ms": 60000}
    _write(path / "capture-ci.json", receipt)
    return path


def _jobs(**attempts):
    return {name: {"name": name, "id": 100 + index, "run_id": 42, "run_attempt": attempts.get(group, 1),
                  "head_sha": SOURCE, "status": "completed", "conclusion": "success"}
            for index, (group, name) in enumerate({**gate.JOBS, "contracts": "visual-contracts"}.items())}


def _needs(requested=False):
    return {"visual-contracts": {"result": "success"}, "visual-capture-execution": {"result": "success"},
            gate.JOBS["all"]: {"result": "success" if requested else "skipped"}}


def _verify(path, inventory, group):
    return gate.validate_receipt(path, inventory=inventory[0], digest=inventory[1], source=SOURCE,
                                 run_id=42, attempt=1, group=group)


def _edit(path, name, change, *, rehash=True):
    value = json.loads((path / name).read_text())
    change(value)
    _write(path / name, value)
    if name != "capture-ci.json" and rehash:
        receipt = json.loads((path / "capture-ci.json").read_text())
        receipt["file_sha256"][name] = gate.sha((path / name).read_bytes())
        _write(path / "capture-ci.json", receipt)


def test_independent_full_inventory_validates_all_five_case_kinds(tmp_path, inventory):
    path = _make(tmp_path, inventory, "all")
    assert len(_verify(path, inventory, "all")["cases"]) == 5


def test_latest_producer_attempts_keep_successful_dependencies_and_pair_control(tmp_path, inventory):
    _make(tmp_path, inventory, "dynamic")
    _make(tmp_path, inventory, "remaining")
    newest = _make(tmp_path, inventory, "remaining", attempt=2)
    _make(tmp_path, inventory, "all")
    chosen, proof = gate.validate_evidence(tmp_path, jobs=_jobs(remaining=2), source=SOURCE, run_id=42,
        current_attempt=2, qualification=True, serial_requested=True, inventory=inventory[0], digest=inventory[1])
    assert chosen["remaining"] == newest
    assert proof["groups"]["dynamic"]["producer_attempt"] == 1
    assert proof["groups"]["remaining"]["producer_attempt"] == 2
    assert proof["selected_count"] == proof["control"]["selected_count"] == 5
    assert proof["control"]["groups"]["all"]["case_count"] == 5
    assert proof["control"]["serial_control"] is True and proof["paired_control"] is True
    assert set(proof["groups"]) == {"dynamic", "remaining"}


def test_gate_generated_control_proof_is_comparator_compatible(tmp_path, inventory):
    from tools.visual_ci_compare import compare_proof

    for group in (*gate.GROUPS, "all"):
        _make(tmp_path, inventory, group)
    _, proof = gate.validate_evidence(tmp_path, jobs=_jobs(), source=SOURCE, run_id=42,
        current_attempt=1, qualification=True, serial_requested=True, inventory=inventory[0], digest=inventory[1])
    comparison = compare_proof(proof)
    assert comparison["selected_count"] == 5
    assert comparison["source_commit"] == SOURCE
    assert comparison["serial_runner_ms"] == proof["control"]["groups"]["all"]["runner_duration_ms"]


@pytest.mark.parametrize("key,value", [
    ("complete", False), ("exit_code", 1), ("retries", 1), ("skips", 1), ("workers", 2), ("workers", True),
    ("source_commit", "b" * 40), ("workflow_run_id", "43"), ("run_attempt", 2), ("platform", "Windows"),
    ("inventory_sha256", "0" * 64), ("logical_job", "unregistered-job"), ("duration_ms", -1),
])
def test_invalid_receipt_cannot_publish_complete(tmp_path, inventory, key, value):
    path = _make(tmp_path / "raw", inventory, "all")
    _edit(path, "capture-ci.json", lambda receipt: receipt.update({key: value}))
    with pytest.raises(gate.VisualGateError):
        gate.publish_complete(path, tmp_path / "complete", inventory=inventory[0], digest=inventory[1], source=SOURCE, run_id=42, attempt=1)
    assert not (tmp_path / "complete").exists()


@pytest.mark.parametrize("mutation", [
    lambda receipt: receipt["cases"].pop(),
    lambda receipt: receipt["cases"].append(copy.deepcopy(receipt["cases"][0])),
    lambda receipt: receipt["cases"][0].update(id="unregistered-case"),
    lambda receipt: receipt["cases"][0].update(outcome="failed"),
    lambda receipt: receipt["cases"][0].update(outcome="incomplete"),
    lambda receipt: receipt["cases"][0]["observations"].pop(),
    lambda receipt: receipt["cases"][0].update(observations=["snapshot:fresh", "loading", "loading"]),
    lambda receipt: receipt["cases"][0]["configuration"].update(fontScale=True),
    lambda receipt: receipt["cases"][0]["files"].append("trace.zip"),
    lambda receipt: receipt["file_sha256"].update({"private.json": "0" * 64}),
])
def test_claimed_coverage_cannot_replace_source_expected_inventory(tmp_path, inventory, mutation):
    path = _make(tmp_path, inventory, "all")
    _edit(path, "capture-ci.json", mutation)
    with pytest.raises(gate.VisualGateError):
        _verify(path, inventory, "all")


@pytest.mark.parametrize("filename,mutation", [
    ("transitions/home.json", lambda report: report["transitions"].pop()),
    ("transitions/home.json", lambda report: report["transitions"][1].update(id="different")),
    ("transitions/home.json", lambda report: report["transitions"][0].pop("before")),
    ("transitions/home.json", lambda report: report.update(fontScale=True)),
    ("transitions/home.json", lambda report: report.update(failures=["failure text must remain private"])),
    ("freshness.json", lambda report: report["transitions"][0].update(to="freshness-0")),
    ("freshness.json", lambda report: report["overlays"].pop()),
    ("freshness.json", lambda report: report["transitions"].reverse()),
    ("freshness.json", lambda report: report.update(expected=True)),
    ("home-fresh-light-desktop-geometry.json", lambda report: report.update(samples=0, frames=[])),
    ("home-fresh-light-desktop-geometry.json", lambda report: report.update(mode="dark")),
    ("home-fresh-light-desktop-geometry.json", lambda report: report["frames"][0].update(errors=["failure"])),
    ("review/acceptance.json", lambda report: report["checks"][0].update(result="failed")),
    ("review/acceptance.json", lambda report: report["checks"].clear()),
    ("static-surfaces/acceptance.json", lambda report: report["entries"].pop()),
    ("static-surfaces/popup-ready-light-390-geometry.json", lambda report: report.update(revision="b" * 40)),
    ("manifest.json", lambda report: report["freshness"].clear()),
    ("manifest.json", lambda report: report["dynamic"][0].update(states=[])),
    ("manifest.json", lambda report: report["entries"].append(copy.deepcopy(report["entries"][0]))),
    ("manifest.json", lambda report: report["review"].update(passed=True)),
    ("manifest.json", lambda report: report["diagnostics"][0].update(type="deuteranopia")),
])
def test_raw_reports_independently_refuse_rehashed_false_green_receipts(tmp_path, inventory, filename, mutation):
    path = _make(tmp_path, inventory, "all")
    _edit(path, filename, mutation)
    with pytest.raises(gate.VisualGateError):
        _verify(path, inventory, "all")


def test_changed_bytes_and_non_png_cannot_enter_complete_artifact(tmp_path, inventory):
    path = _make(tmp_path, inventory, "all")
    image = "review/rail-light.png"
    (path / image).write_bytes(b"not an image")
    with pytest.raises(gate.VisualGateError, match="file_digest"):
        _verify(path, inventory, "all")
    _edit(path, "capture-ci.json", lambda receipt: receipt["file_sha256"].update({image: gate.sha((path / image).read_bytes())}))
    with pytest.raises(gate.VisualGateError, match="invalid_png"):
        _verify(path, inventory, "all")


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_newer_producer_failure_never_reuses_old_green(conclusion):
    jobs = _jobs(dynamic=2)
    jobs[gate.JOBS["all"]]["conclusion"] = "skipped"
    jobs[gate.JOBS["dynamic"]]["conclusion"] = conclusion
    with pytest.raises(gate.VisualGateError, match="latest_visual_job_failed"):
        gate.validate_jobs(_needs(), jobs)


def test_only_unrequested_control_may_skip():
    jobs = _jobs()
    jobs[gate.JOBS["all"]]["conclusion"] = "skipped"
    gate.validate_jobs(_needs(), jobs)
    with pytest.raises(gate.VisualGateError):
        gate.validate_jobs(_needs(True), jobs, serial_requested=True, qualification=True)
    with pytest.raises(gate.VisualGateError):
        gate.validate_jobs(_needs(True), _jobs(), serial_requested=True, qualification=False)


@pytest.mark.parametrize("failure", ["missing", "stale", "duplicate", "wrong-name", "unexpected-control"])
def test_artifact_execution_identity_and_exact_partition(tmp_path, inventory, failure):
    _make(tmp_path, inventory, "dynamic")
    if failure != "missing":
        path = _make(tmp_path, inventory, "remaining")
    jobs = _jobs(remaining=2 if failure == "stale" else 1)
    if failure == "duplicate":
        import shutil
        shutil.copytree(path, tmp_path / "duplicate")
    elif failure == "wrong-name":
        path.rename(tmp_path / "wrong-name")
    elif failure == "unexpected-control":
        _make(tmp_path, inventory, "all")
    with pytest.raises(gate.VisualGateError):
        gate.validate_evidence(tmp_path, jobs=jobs, source=SOURCE, run_id=42, current_attempt=2,
            qualification=False, inventory=inventory[0], digest=inventory[1])


@pytest.mark.parametrize("failure", ["missing-receipt", "malformed", "private-error", "private-id", "crash", "cancelled"])
def test_partial_publication_cannot_leak_raw_failure_artifacts(tmp_path, inventory, failure, monkeypatch, capsys):
    path = _make(tmp_path / "raw", inventory, "all")
    canary = "private-account-value-do-not-publish"
    if failure == "missing-receipt":
        (path / "capture-ci.json").unlink()
    elif failure == "malformed":
        (path / "capture-ci.json").write_text('{"error":"' + canary)
    elif failure == "private-error":
        _edit(path, "capture-ci.json", lambda receipt: receipt["cases"][0].update(error=canary))
    elif failure == "private-id":
        _edit(path, "capture-ci.json", lambda receipt: receipt["cases"][0].update(id=canary))
    else:
        _edit(path, "capture-ci.json", lambda receipt: receipt.update(complete=False, exit_code=1))
        _edit(path, "capture-ci.json", lambda receipt: receipt["cases"][0].update(outcome="incomplete" if failure == "crash" else "failed"))
    (path / "failure.png").write_bytes(canary.encode())
    (path / "error.log").write_text(canary)
    destination = tmp_path / "diagnostics"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    gate.publish_diagnostics(path, destination, inventory[0], source=SOURCE, run_id=42, attempt=1)
    assert sorted(file.name for file in destination.iterdir()) == ["diagnostics.json"]
    assert canary not in (destination / "diagnostics.json").read_text()
    assert canary not in summary.read_text()
    assert canary not in capsys.readouterr().out
    result = json.loads((destination / "diagnostics.json").read_text())
    assert result["source_commit"] == SOURCE
    if failure in {"crash", "cancelled"}:
        assert result["status"] == "restricted_metadata"
        assert all(set(row) == {"id", "outcome"} for row in result["cases"])
        assert inventory[0]["cases"][0]["id"] in summary.read_text()
        assert "--stage-group all" in summary.read_text()
    else:
        assert "restricted_metadata_unavailable" in summary.read_text()


def test_generic_gate_failure_summary_has_only_closed_reason_and_rerun_commands(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("PRODUCT_SHA", "private-invalid-source-value")
    result = gate.main(["--reports-dir", str(tmp_path), "--output-dir", str(tmp_path / "complete"),
                        "--verified-output", str(tmp_path / "proof.json")])
    assert result == 1
    text = summary.read_text()
    assert "invalid_source" in text
    assert "private-invalid-source-value" not in text
    assert "--stage-group dynamic" in text and "--stage-group remaining" in text
    assert not (tmp_path / "complete").exists()


@pytest.mark.parametrize("phases", [{"prepare": 1}, {"private-phase-canary": 1}, {"prepare": True}, {"prepare": -1}])
def test_partial_phase_summary_keeps_only_closed_names_and_valid_durations(tmp_path, inventory, monkeypatch, phases):
    raw = _make(tmp_path / "inputs", inventory, "dynamic")
    receipt = json.loads((raw / "capture-ci.json").read_text())
    receipt["cases"][0]["outcome"] = "failed"
    receipt["phase_timings"] = phases
    _write(raw / "capture-ci.json", receipt)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    result = gate.publish_diagnostics(raw, tmp_path / "diagnostics", inventory[0], source=SOURCE, run_id=42, attempt=1)
    if phases == {"prepare": 1} and type(phases["prepare"]) is int:
        assert result["phase_timings"] == phases
        assert "prepare=1.000" in summary.read_text()
    else:
        assert result["status"] == "metadata_unavailable"
        assert result["phase_timings"] == {}
    assert "private-phase-canary" not in summary.read_text()


def test_assembly_preserves_approved_files_and_finalize_binds_color(tmp_path, inventory, monkeypatch):
    for group in gate.GROUPS:
        _make(tmp_path / "inputs", inventory, group)
    chosen, proof = gate.validate_evidence(tmp_path / "inputs", jobs=_jobs(), source=SOURCE, run_id=42,
        current_attempt=1, qualification=False, inventory=inventory[0], digest=inventory[1])
    output, proof_path = tmp_path / "complete", tmp_path / "verified.json"
    proof = gate.assemble(chosen, output, inventory[0], proof)
    _write(proof_path, proof)
    assert (output / "transitions/home.json").read_bytes() == (chosen["dynamic"] / "transitions/home.json").read_bytes()
    assert (output / "freshness.json").read_bytes() == (chosen["remaining"] / "freshness.json").read_bytes()
    assert not (output / "capture-ci.json").exists()
    for key, value in {"PRODUCT_SHA": SOURCE, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(key, value)
    _write(output / "color-qualification.json", {"checked": True})
    receipt = gate.finalize(output, proof_path)
    assert "color-qualification.json" in receipt["file_sha256"]
    assert json.loads(proof_path.read_text())["finalized"] is True
    assert receipt["producers"]["dynamic"]["producer_attempt"] == 1
    with pytest.raises(gate.VisualGateError):
        gate.finalize(output, proof_path)


def test_finalize_refuses_unapproved_file_and_modified_assembled_bytes(tmp_path, inventory, monkeypatch):
    for group in gate.GROUPS:
        _make(tmp_path / "inputs", inventory, group)
    chosen, proof = gate.validate_evidence(tmp_path / "inputs", jobs=_jobs(), source=SOURCE, run_id=42,
        current_attempt=1, qualification=False, inventory=inventory[0], digest=inventory[1])
    output, proof_path = tmp_path / "complete", tmp_path / "verified.json"
    _write(proof_path, gate.assemble(chosen, output, inventory[0], proof))
    for key, value in {"PRODUCT_SHA": SOURCE, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(key, value)
    _write(output / "color-qualification.json", {})
    (output / "trace.zip").write_bytes(b"unapproved")
    with pytest.raises(gate.VisualGateError, match="file_inventory"):
        gate.finalize(output, proof_path)
    assert not (output / "ci-receipt.json").exists()
    (output / "trace.zip").unlink()
    (output / "freshness.json").write_text("{}")
    with pytest.raises(gate.VisualGateError, match="assembly_modified"):
        gate.finalize(output, proof_path)


def test_source_policy_matches_complete_current_visual_workflow():
    assert gate.load_policy() == gate.POLICY


@pytest.mark.parametrize("old,new", [
    ("    name: visual-capture-${{ matrix.group }}", "    name: visual-capture-dynamic"),
    ("      max-parallel: 2", "      max-parallel: 3"),
    ("group: [dynamic, remaining]", "group: [dynamic, dynamic]"),
    ("    strategy:", "    needs: visual-contracts\n    strategy:"),
    ("    if: ${{ always() }}", "    if: success()"),
    (gate.SERIAL_IF, "${{ always() }}"),
])
def test_source_policy_rejects_coverage_or_aggregate_weakening(old, new):
    source = (gate.ROOT / gate.POLICY["workflow"]).read_text()
    # Limit serial condition mutation to the job, leaving the top-level env intact.
    if old == gate.SERIAL_IF:
        before, body = source.split("  visual-capture-serial-control:\n", 1)
        source = before + "  visual-capture-serial-control:\n" + body.replace(old, new, 1)
    else:
        assert old in source
        source = source.replace(old, new, 1)
    with pytest.raises((gate.VisualGateError, gate.ContractError)):
        gate.validate_policy(source.encode(), (gate.ROOT / "ci/visual-ci-policy.json").read_bytes())
