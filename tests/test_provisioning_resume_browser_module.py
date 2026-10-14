from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["setup-transfer.test.mjs", "validation.test.mjs"])
def test_explicit_recovery_browser_module(module: str) -> None:
    """Keep explicit transfer/parser coverage in the ordinary CI test matrix."""

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
