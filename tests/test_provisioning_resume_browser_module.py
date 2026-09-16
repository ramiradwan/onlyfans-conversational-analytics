from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_provisioning_resume_browser_module() -> None:
    """Keep restart/resume controller coverage in the ordinary CI test matrix."""

    subprocess.run(
        [
            "node",
            "--test",
            str(ROOT / "app" / "provisioning" / "provisioning-resume.test.mjs"),
        ],
        cwd=ROOT,
        check=True,
        text=True,
    )
