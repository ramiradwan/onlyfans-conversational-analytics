from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tools" / "retention-evidence" / "execute-ret-001-current-behavior-observations.py"
_SPEC = importlib.util.spec_from_file_location("ret001_current_behavior", RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

REVISION = "1" * 40


def test_companion_route_table_characterizes_absent_selective_delete_control():
    result = runner.observe_companion_delete_route_surface(REVISION)
    assert result["capability_kind"] == "NEGATIVE_CAPABILITY"
    assert result["factual_result"] == "NO_SELECTIVE_DATA_DELETE_HTTP_CONTROL_OBSERVED"
    assert result["facts"]["delete_routes"] == [
        {
            "path": "/api/v1/settings/history/consent",
            "methods": ["DELETE"],
            "router_module": "app.api.endpoints.history",
        }
    ]
    assert result["facts"]["sole_delete_route_semantics"] == "history_consent_revocation"
    assert result["facts"]["message_delete_route_present"] is False
    assert result["facts"]["conversation_delete_route_present"] is False
    assert result["facts"]["dataset_delete_route_present"] is False
    assert result["execution_context"]["production_deletion_api_added"] is False


def test_existing_bridge_reset_removes_projection_without_deleting_canonical(tmp_path):
    result = runner.observe_bridge_projection_reset(tmp_path, REVISION)
    assert result["capability_kind"] == "POSITIVE_CAPABILITY"
    assert result["factual_result"] == "GLOBAL_BRIDGE_DERIVED_RESET_OBSERVED"
    assert result["facts"]["canonical_survived"] is True
    assert result["facts"]["derived_projection_removed"] is True
    assert result["facts"]["projection_message_after_reset"] is None
    assert result["facts"]["selective_message_delete_control_observed"] is False
    assert result["execution_context"]["production_retention_or_deletion_behavior_added"] is False


def test_history_revocation_changes_authorization_state_without_deleting_existing_data(tmp_path):
    result = runner.observe_history_revocation_preserves_data(tmp_path, REVISION)
    assert result["capability_kind"] == "NEGATIVE_CAPABILITY"
    assert result["factual_result"] == "HISTORY_REVOCATION_PRESERVES_EXISTING_DATA_OBSERVED"
    assert result["facts"]["settings_state_after_revocation"] == "revoked"
    assert result["facts"]["consent_revision_after_revocation"] is None
    assert result["facts"]["authorized_platform_creator_id_after_revocation"] is None
    assert result["facts"]["canonical_message_preserved"] is True
    assert result["facts"]["projection_message_preserved"] is True
    assert (
        result["facts"]["canonical_message_before"]["text_sha256"]
        == result["facts"]["canonical_message_after_revocation"]["text_sha256"]
    )
    assert (
        result["facts"]["projection_message_before"]["text_sha256"]
        == result["facts"]["projection_message_after_revocation"]["text_sha256"]
    )
