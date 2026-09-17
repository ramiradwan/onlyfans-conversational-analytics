"""Tests for the customer journey manifest and its validator."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from tools.validate_ux_journey import UxJourneyError, load_manifest, main, validate_manifest


@pytest.fixture(scope="module")
def manifest() -> dict:
    return load_manifest()


def _state(manifest: dict, state_id: str) -> dict:
    return next(state for state in manifest["states"] if state["id"] == state_id)


def test_repository_manifest_is_valid(manifest: dict) -> None:
    validate_manifest(manifest)


def test_cli_reports_valid_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "UX journey manifest valid" in capsys.readouterr().out


def test_every_extension_customer_state_is_mapped(manifest: dict) -> None:
    source = (ROOT / "extension" / "runtime" / "customer-journey.mjs").read_text(encoding="utf-8")
    declared = source.split("CUSTOMER_STATES = Object.freeze({", 1)[1].split("});", 1)[0]
    constants = {line.split(":", 1)[0].strip() for line in declared.splitlines() if ":" in line}
    anchored = {
        state["anchor"]["text"].split(":", 1)[0]
        for state in manifest["states"]
        if state["anchor"]["file"] == "extension/runtime/customer-journey.mjs"
    }
    assert constants
    assert constants == anchored


def _mutations() -> dict[str, tuple]:
    def unknown_target(m: dict) -> None:
        _state(m, "extension.paused")["transitions"][0]["to"] = "extension.missing"

    def self_target(m: dict) -> None:
        _state(m, "extension.paused")["transitions"][0]["to"] = "extension.paused"

    def missing_implementation_file(m: dict) -> None:
        m["surfaces"]["desktop.app"]["implementation"].append("frontend/src/Missing.tsx")

    def missing_anchor_text(m: dict) -> None:
        _state(m, "desktop.stored_messages")["anchor"]["text"] = "'retired_stage'"

    def copy_anchor(m: dict) -> None:
        _state(m, "desktop.setup_prompt")["anchor"]["text"] = "Continue setup"

    def foreign_attribute_anchor(m: dict) -> None:
        _state(m, "desktop.setup_prompt")["anchor"]["text"] = 'data-journey-state="desktop.numbers_ready"'

    def anchor_outside_surface(m: dict) -> None:
        _state(m, "desktop.stored_messages")["anchor"] = {"file": "extension/popup.html", "text": "id=\"pre-mode\""}

    def missing_evidence_test(m: dict) -> None:
        _state(m, "extension.full_ready")["evidence"][0]["name"] = "a test that does not exist"

    def evidence_substring_not_title(m: dict) -> None:
        _state(m, "extension.full_ready")["evidence"][0]["name"] = "FULL_READY"

    def evidence_locator_not_scenario(m: dict) -> None:
        _state(m, "desktop.stored_messages")["evidence"][1]["name"] = "Delete all messages"

    def empty_evidence(m: dict) -> None:
        _state(m, "extension.full_ready")["evidence"] = []

    def unknown_evidence_kind(m: dict) -> None:
        _state(m, "extension.full_ready")["evidence"][0]["kind"] = "manual"

    def user_transition_without_action(m: dict) -> None:
        del _state(m, "extension.paused")["transitions"][0]["action"]

    def system_transition_with_action(m: dict) -> None:
        _state(m, "desktop.history_syncing")["transitions"][0]["action"] = "wait"

    def unreachable_success(m: dict) -> None:
        _state(m, "desktop.passkey_sign_in")["transitions"] = []

    def orphan_state(m: dict) -> None:
        orphan = copy.deepcopy(_state(m, "extension.full_ready"))
        orphan["id"] = "extension.orphan"
        m["states"].append(orphan)

    def duplicate_state(m: dict) -> None:
        m["states"].append(copy.deepcopy(_state(m, "desktop.stored_messages")))

    def parent_path(m: dict) -> None:
        _state(m, "desktop.stored_messages")["evidence"][0]["file"] = "../README.md"

    def unknown_key(m: dict) -> None:
        _state(m, "desktop.stored_messages")["note"] = "free text"

    def surface_without_states(m: dict) -> None:
        m["surfaces"]["desktop.tray"] = {"implementation": ["frontend/src/views/SettingsView.tsx"]}

    return {
        name: (function, pattern)
        for name, function, pattern in [
            ("unknown_target", unknown_target, "unknown state"),
            ("self_target", self_target, "its own state"),
            ("missing_implementation_file", missing_implementation_file, "does not exist"),
            ("missing_anchor_text", missing_anchor_text, "is not present"),
            ("copy_anchor", copy_anchor, "must be a code identifier"),
            ("foreign_attribute_anchor", foreign_attribute_anchor, "must name its own state"),
            ("anchor_outside_surface", anchor_outside_surface, "not an implementation file"),
            ("missing_evidence_test", missing_evidence_test, "is not a test title"),
            ("evidence_substring_not_title", evidence_substring_not_title, "is not a test title"),
            ("evidence_locator_not_scenario", evidence_locator_not_scenario, "is not a capture scenario"),
            ("empty_evidence", empty_evidence, "evidence must be a non-empty list"),
            ("unknown_evidence_kind", unknown_evidence_kind, "kind must be one of"),
            ("user_transition_without_action", user_transition_without_action, "action must be a non-empty string"),
            ("system_transition_with_action", system_transition_with_action, "system transition with an action"),
            ("unreachable_success", unreachable_success, "cannot reach desktop.numbers_ready"),
            ("orphan_state", orphan_state, "not reachable from any journey entry"),
            ("duplicate_state", duplicate_state, "Duplicate state ids"),
            ("parent_path", parent_path, "repository-relative POSIX path"),
            ("unknown_key", unknown_key, "unknown keys"),
            ("surface_without_states", surface_without_states, "Surfaces without states"),
        ]
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_invalid_manifest_is_rejected(manifest: dict, name: str) -> None:
    mutate, pattern = _mutations()[name]
    broken = copy.deepcopy(manifest)
    mutate(broken)
    with pytest.raises(UxJourneyError, match=pattern):
        validate_manifest(broken)


def _copy_referenced_files(manifest: dict, target: Path) -> None:
    files = {file for surface in manifest["surfaces"].values() for file in surface["implementation"]}
    for state in manifest["states"]:
        files.add(state["anchor"]["file"])
        files.update(item["file"] for item in state["evidence"])
    for relative in files:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())


@pytest.mark.parametrize(
    ("file", "state_id", "pattern"),
    [
        ("frontend/src/components/SetupPrompt.tsx", "desktop.retired", "renders undeclared journey state"),
        ("frontend/src/views/SettingsView.tsx", "desktop.stored_messages", "which is anchored in"),
        ("frontend/src/views/UnlistedView.tsx", "desktop.setup_prompt", "which is anchored in"),
    ],
)
def test_rendered_journey_attribute_must_match_the_manifest(
    manifest: dict, tmp_path: Path, file: str, state_id: str, pattern: str
) -> None:
    _copy_referenced_files(manifest, tmp_path)
    validate_manifest(manifest, tmp_path)
    path = tmp_path / file
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + f'\nexport const Stray = () => <div data-journey-state="{state_id}" />;\n', encoding="utf-8")
    with pytest.raises(UxJourneyError, match=pattern):
        validate_manifest(manifest, tmp_path)
