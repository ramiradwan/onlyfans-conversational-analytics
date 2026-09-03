"""Keeps the provisioning rehearsal stage reachable from the default suite run.

``tests/test_ci_workflow.py`` asserts that a Windows job runs ``npm test`` in
``tools/e2e-capture``. That command reaches this stage only while the spec sits
under the configured ``testDir`` and the script stays an unfiltered
``playwright test``, so both are asserted here: a stage nothing runs proves
nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
E2E_ROOT = PRODUCT_ROOT / "tools" / "e2e-capture"
PLAYWRIGHT_CONFIG = E2E_ROOT / "playwright.config.mjs"
PACKAGE_MANIFEST = E2E_ROOT / "package.json"

STAGE_SPEC_NAME = "provisioning-registration.spec.mjs"

# Arguments that would narrow `playwright test` to a subset of the specs.
SELECTION_ARGUMENTS = ("--grep", "-g", "--project", "--shard", "--last-failed")


def _configured_test_directory() -> Path:
    source = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"testDir:\s*'([^']+)'", source)
    assert match is not None, "playwright.config.mjs does not declare a testDir"
    return (E2E_ROOT / match.group(1)).resolve()


def _default_test_script() -> str:
    manifest = json.loads(PACKAGE_MANIFEST.read_text(encoding="utf-8"))
    script = manifest.get("scripts", {}).get("test")
    assert isinstance(script, str), "tools/e2e-capture declares no `test` script"
    return script


def test_the_stage_spec_sits_under_the_configured_test_directory() -> None:
    spec = _configured_test_directory() / STAGE_SPEC_NAME

    assert spec.is_file(), (
        f"`{STAGE_SPEC_NAME}` is not under the configured testDir "
        f"`{_configured_test_directory()}`, so `playwright test` never runs it"
    )


def test_the_default_test_script_selects_every_spec() -> None:
    script = _default_test_script()
    words = script.split()

    assert words[:2] == ["playwright", "test"], (
        f"the `test` script is `{script}`, which is not a `playwright test` run"
    )
    narrowing = [word for word in words[2:] if word in SELECTION_ARGUMENTS]
    assert not narrowing, (
        f"the `test` script narrows the run with {narrowing}, so specs outside "
        f"that selection stop executing in CI"
    )
    paths = [word for word in words[2:] if not word.startswith("-")]
    assert not paths, (
        f"the `test` script names {paths}, so only those specs execute in CI"
    )


def test_the_stage_drives_registration_and_local_authentication() -> None:
    spec = _configured_test_directory() / STAGE_SPEC_NAME
    source = spec.read_text(encoding="utf-8")

    for symbol in ("ProvisioningHost", "completeBrowserWebAuthnCeremony"):
        assert symbol in source, (
            f"`{STAGE_SPEC_NAME}` no longer uses `{symbol}`, so the stage stops "
            f"covering the step it names"
        )
