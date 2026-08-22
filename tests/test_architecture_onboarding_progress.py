"""Structural A8 caller and one-way-boundary guard."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
CALLER_FILES = {
    "installed": ROOT / "app/persistence/auth.py",
    "enrolled": ROOT / "app/persistence/auth.py",
    "account-bound": ROOT / "app/persistence/auth.py",
    "first-capture-ready": ROOT / "app/transport/manager.py",
}


def test_each_milestone_has_the_named_non_test_production_caller() -> None:
    sources = {path: path.read_text("utf-8") for path in set(CALLER_FILES.values())}

    for milestone, path in CALLER_FILES.items():
        assert milestone in sources[path], f"missing production caller for {milestone}"
    auth_source = sources[CALLER_FILES["installed"]]
    record_claim = auth_source.rindex("def record_claim_submission(")
    resolve_claim = auth_source.rindex("def resolve_claim_submission(")
    activate_pairing = auth_source.rindex("def activate_agent_pairing(")
    issue_challenge = auth_source.index("def issue_webauthn_challenge(", activate_pairing)
    assert '"installed",' in auth_source[record_claim:resolve_claim]
    assert '"enrolled",' in auth_source[resolve_claim:]
    assert "state is ClaimSubmissionState.CONSUMED" in auth_source[resolve_claim:]
    assert '"account-bound",' in auth_source[activate_pairing:issue_challenge]
    assert "pairing.activated_at IS NOT NULL" in auth_source
    assert 'milestone == "first-capture-ready"' in auth_source
    assert "LICENSE_ENTITLEMENT" in auth_source
    assert "self._onboarding_progress.mark," in sources[
        CALLER_FILES["first-capture-ready"]
    ]


def test_hosted_reporting_cannot_poll_or_import_runtime_surfaces() -> None:
    reporting_files = (
        ROOT / "app/security/hosted_grants.py",
        ROOT / "app/provisioning/progress_reporting.py",
    )
    forbidden_imports = (
        "app.api",
        "app.analytics",
        "app.protocol",
        "app.services",
        "app.transport",
    )

    for path in reporting_files:
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for name in imported
            for prefix in forbidden_imports
        ), f"{path} imports a Brain/Bridge/Agent runtime surface"

    hosted_source = (ROOT / "app/security/hosted_grants.py").read_text("utf-8")
    assert 'self._transport.request(\n                "POST"' in hosted_source
    assert 'self._transport.request(\n                "GET"' not in hosted_source


def test_reporting_has_no_authorization_or_existing_data_control_path() -> None:
    source = (ROOT / "app/provisioning/progress_reporting.py").read_text("utf-8")
    forbidden = {
        "RuntimePolicy",
        "runtime_is_activated",
        "evaluate_runtime_activation",
        "capture_policy",
        "viewing",
        "export",
        "backup",
        "deletion",
        "canonical_database",
        "projection",
    }
    assert forbidden.isdisjoint(source.split())

    independent_surfaces = (
        ROOT / "app/security/activation_gate.py",
        ROOT / "app/services/data_ingest.py",
        ROOT / "app/transport/ingestion.py",
        ROOT / "app/api/endpoints/frontend.py",
        ROOT / "app/api/endpoints/history.py",
        ROOT / "app/api/endpoints/insights.py",
        ROOT / "app/persistence/backup.py",
    )
    for path in independent_surfaces:
        assert "progress_reporting" not in path.read_text("utf-8"), str(path)
