"""Guards the e2e-only installation-key seam from migrating into the product.

``tools/e2e-capture/helpers/seed_webauthn_grants.py`` drives a synthetic
installation key through the real persistence transitions so the Linux
``capture-e2e`` job can seed WebAuthn grants on a host with no TPM-backed
provider. It ships with nothing and depends on ``app``, not the other way
round: nothing under ``app`` may reference it, or the synthetic-key seam
could silently reach a real installation.
"""

from __future__ import annotations

import ast
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PRODUCT_ROOT / "app"

E2E_HELPER_NAME = "seed_webauthn_grants"


def _e2e_helper_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    references: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if E2E_HELPER_NAME in alias.name:
                    references.add(f"imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and E2E_HELPER_NAME in node.module:
                references.add(f"imports from {node.module}")
            for alias in node.names:
                if E2E_HELPER_NAME in alias.name:
                    references.add(f"imports {alias.name}")
        elif isinstance(node, ast.Name) and node.id == E2E_HELPER_NAME:
            references.add(f"references {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr == E2E_HELPER_NAME:
            references.add(f"references {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if E2E_HELPER_NAME in node.value:
                references.add(f"names {E2E_HELPER_NAME}")
    return sorted(references)


def _e2e_helper_violations(brain_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(brain_root.rglob("*.py")):
        references = _e2e_helper_references(path)
        if references:
            violations.append(
                f"{path.relative_to(brain_root).as_posix()}: {references}"
            )
    return violations


def test_no_app_module_references_the_e2e_grant_seeding_helper() -> None:
    violations = _e2e_helper_violations(BRAIN_ROOT)

    assert not violations, (
        "A module under app/ references the e2e-only grant seeding helper:\n"
        + "\n".join(violations)
    )


def test_guard_rejects_e2e_helper_reference_in_an_app_module(tmp_path: Path) -> None:
    path = tmp_path / "app" / "security" / "example.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from tools.e2e_capture.helpers.seed_webauthn_grants import main\n",
        encoding="utf-8",
    )

    assert _e2e_helper_violations(tmp_path / "app") == [
        "security/example.py: "
        "['imports from tools.e2e_capture.helpers.seed_webauthn_grants']"
    ]
