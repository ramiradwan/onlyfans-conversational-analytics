from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _browser_job() -> dict:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["windows-browser-e2e"]
    assert str(job["runs-on"]).startswith("windows")
    return job


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_windows_browser_e2e_keeps_package_and_browser_caches_on_runner_storage() -> None:
    job = _browser_job()

    storage = _step(job, "Keep browser test caches on runner storage")
    command = storage["run"]
    assert "$env:RUNNER_TEMP" in command
    for variable in ("TMPDIR", "TEMP", "TMP", "NPM_CONFIG_CACHE", "PLAYWRIGHT_BROWSERS_PATH"):
        assert variable in command

    cache = _step(job, "Cache Playwright Chromium")
    assert str(cache["uses"]).startswith("actions/cache@")
    assert cache["with"]["path"] == r"${{ runner.temp }}\ms-playwright"
    key = cache["with"]["key"]
    assert "${{ runner.os }}" in key
    assert "${{ runner.arch }}" in key
    assert "tools/e2e-capture/package-lock.json" in key
    assert "restore-keys" not in cache["with"]

    install = _step(job, "Install Playwright Chromium")
    assert install["if"] == "steps.playwright-cache.outputs.cache-hit != 'true'"
