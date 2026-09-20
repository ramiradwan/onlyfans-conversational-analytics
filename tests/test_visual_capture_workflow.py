from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
VISUAL_WORKFLOW = ROOT / ".github" / "workflows" / "visual-capture.yml"


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path} is not a mapping document"
    return document


def test_visual_capture_is_not_part_of_release_qualified_ci() -> None:
    ci = _load(CI_WORKFLOW)
    assert "visual-capture" not in ci["jobs"]


def test_visual_capture_runs_only_for_relevant_ui_changes() -> None:
    workflow = yaml.load(VISUAL_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    for event in ("push", "pull_request"):
        paths = set(workflow["on"][event]["paths"])
        assert "frontend/**" in paths
        assert {"extension/popup.html", "extension/popup.css", "extension/popup.js",
                "extension/runtime/customer-journey.mjs", "app/provisioning/provisioning.html",
                "app/provisioning/provisioning.js"} <= paths
        assert {"extension/setup.*", "extension/options.*", "extension/ui/**"} <= paths
        assert "tools/visual-capture/**" in paths
        assert ".github/workflows/visual-capture.yml" in paths
    assert "edited" not in workflow["on"]["pull_request"]["types"]


def test_visual_capture_caches_chromium_by_lockfile() -> None:
    job = _load(VISUAL_WORKFLOW)["jobs"]["visual-capture"]
    assert job["timeout-minutes"] == 20
    steps = job["steps"]
    cache = next(step for step in steps if step.get("id") == "playwright-cache")
    assert cache["with"]["path"] == "~/.cache/ms-playwright"
    key = cache["with"]["key"]
    assert "${{ runner.os }}" in key
    assert "${{ runner.arch }}" in key
    assert "tools/visual-capture/package-lock.json" in key
    install = next(step for step in steps if step.get("name") == "Install Playwright Chromium")
    assert install["if"] == "steps.playwright-cache.outputs.cache-hit != 'true'"


def test_visual_capture_artifact_is_bound_to_the_product_revision() -> None:
    workflow = _load(VISUAL_WORKFLOW)
    assert workflow["env"]["PRODUCT_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    job = workflow["jobs"]["visual-capture"]
    checkout = next(step for step in job["steps"] if step.get("name") == "Checkout exact Product revision")
    assert checkout["with"]["ref"] == "${{ env.PRODUCT_SHA }}"
    upload = next(step for step in job["steps"] if step.get("name") == "Upload visual states")
    assert upload["with"]["name"] == "visual-states-${{ env.PRODUCT_SHA }}"
    assert upload["with"]["retention-days"] == 30


def test_visual_capture_qualifies_colors_and_diagnostic_cleanup() -> None:
    steps = _load(VISUAL_WORKFLOW)["jobs"]["visual-capture"]["steps"]
    commands = {step.get("name"): step.get("run", "") for step in steps}
    assert commands["Test visual capture contracts"] == "node --test tools/visual-capture/*.test.mjs"
    assert "--color-report ../artifacts/visual-capture/color-qualification.json" in commands["Record color qualification"]
    names = [step.get("name") for step in steps]
    assert names.index("Capture visual states") < names.index("Record color qualification") < names.index("Upload visual states")
