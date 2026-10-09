from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["provisioning-resume.test.mjs", "native-return.test.mjs"])
def test_provisioning_resume_browser_module(module: str) -> None:
    """Keep restart/resume controller coverage in the ordinary CI test matrix."""

    subprocess.run(
        [
            "node",
            "--test",
            str(ROOT / "app" / "provisioning" / module),
        ],
        cwd=ROOT,
        check=True,
        text=True,
    )
