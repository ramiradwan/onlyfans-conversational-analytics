from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from contracts.loader import (
    ContractsIntegrityError,
    load_trust_set,
    verify_manifest,
    verify_snapshot_integrity,
)


ROOT = Path(__file__).resolve().parents[1] / "contracts"


@pytest.mark.contract_integrity
def test_selected_snapshot_matches_its_independent_consumer_pin() -> None:
    manifest = verify_snapshot_integrity(ROOT)
    assert len(manifest["files"]) == 365
    assert manifest["profiles"] == ["urn:bridge-clean:grant-profile:v1"]
    assert manifest["export_set"] == ["grant-profile-v1"]


@pytest.mark.contract_integrity
def test_fixture_trust_set_is_fail_closed_outside_development() -> None:
    with pytest.raises(ContractsIntegrityError, match="not production usable"):
        load_trust_set("grant-profile-v1/keys/trust-set.json")


@pytest.mark.contract_integrity
def test_fixture_trust_set_can_be_loaded_only_in_development() -> None:
    trust_set = load_trust_set("grant-profile-v1/keys/trust-set.json", environment="development")
    assert trust_set["production_usable"] is False


@pytest.mark.contract_integrity
def test_integrity_check_names_a_fixture_converted_to_crlf(tmp_path: Path) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    fixture = snapshot / "grant-profile-v1" / "creator_account_binding" / "valid-current" / "payload.json"
    fixture.write_bytes(fixture.read_bytes().replace(b"\n", b"\r\n"))

    with pytest.raises(ContractsIntegrityError, match="digest mismatch: grant-profile-v1/creator_account_binding/valid-current/payload.json"):
        verify_manifest(snapshot)


@pytest.mark.contract_integrity
def test_integrity_check_allows_selected_export_when_held_subtrees_are_absent(tmp_path: Path) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(snapshot / "capability-permit-v1")
    shutil.rmtree(snapshot / "permit-consumption")
    shutil.rmtree(snapshot / "schemas")

    manifest = verify_snapshot_integrity(snapshot)
    assert manifest["export_set"] == ["grant-profile-v1"]
