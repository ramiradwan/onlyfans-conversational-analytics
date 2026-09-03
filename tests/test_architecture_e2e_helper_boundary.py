"""Guards the e2e-only installation-key seams from migrating into the product.

``tools/e2e-capture/helpers/seed_webauthn_grants.py`` drives a synthetic
installation key through the real persistence transitions so the Linux
``capture-e2e`` job can seed WebAuthn grants on a host with no TPM-backed
provider. ``tools/e2e-capture/helpers/provisioning_grant_authority.py`` serves
the provisioning surface over a trust set minted in the same process. Both ship
with nothing and depend on ``app``, not the other way round: nothing under
``app`` may reference either, or a synthetic key or a locally minted trust set
could silently reach a real installation.
"""

from __future__ import annotations

import ast
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PRODUCT_ROOT / "app"

E2E_HELPER_NAMES = ("provisioning_grant_authority", "seed_webauthn_grants")


def _e2e_helper_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    references: set[str] = set()

    def record(text: str, template: str) -> None:
        for helper in E2E_HELPER_NAMES:
            if helper in text:
                references.add(template.format(text=text, helper=helper))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.name, "imports {text}")
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                record(node.module, "imports from {text}")
            for alias in node.names:
                record(alias.name, "imports {text}")
        elif isinstance(node, ast.Name):
            record(node.id, "references {text}")
        elif isinstance(node, ast.Attribute):
            record(node.attr, "references {text}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            record(node.value, "names {helper}")
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


def test_no_app_module_references_an_e2e_helper() -> None:
    violations = _e2e_helper_violations(BRAIN_ROOT)

    assert not violations, (
        "A module under app/ references an e2e-only helper:\n"
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


def test_guard_rejects_the_provisioning_authority_in_an_app_module(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app" / "provisioning" / "example.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from tools.e2e_capture.helpers import provisioning_grant_authority\n",
        encoding="utf-8",
    )

    assert _e2e_helper_violations(tmp_path / "app") == [
        "provisioning/example.py: ['imports provisioning_grant_authority']"
    ]
