"""The current browser rehearsal suite remains reachable from its default command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
E2E_ROOT = PRODUCT_ROOT / "tools" / "e2e-capture"
PLAYWRIGHT_CONFIG = E2E_ROOT / "playwright.config.mjs"
PACKAGE_MANIFEST = E2E_ROOT / "package.json"


# Arguments that would narrow `playwright test` to a subset of the specs.
SELECTION_ARGUMENTS = ("--grep", "-g", "--project", "--shard", "--last-failed")


def _default_test_script() -> str:
    manifest = json.loads(PACKAGE_MANIFEST.read_text(encoding="utf-8"))
    script = manifest.get("scripts", {}).get("test")
    assert isinstance(script, str), "tools/e2e-capture declares no `test` script"
    return script


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
