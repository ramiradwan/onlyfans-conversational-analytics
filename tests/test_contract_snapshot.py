from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tools.regenerate_contract_snapshot import (
    EXPECTED_SCHEMA_CLOSURE_SIZE,
    copy_schema_closure,
    schema_dependency_closure,
)
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
    assert len(manifest["files"]) == 427
    assert manifest["profiles"] == [
        "urn:bridge-clean:grant-profile:v1",
        "urn:bridge-clean:capability-permit-v1",
        "urn:bridge-clean:capability-permit-consumption-policy:v1",
    ]
    assert manifest["export_set"] == [
        "grant-profile-v1",
        "capability-permit-v1",
        "permit-consumption",
        "schemas",
    ]


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
def test_manifest_covers_every_selected_export_root() -> None:
    manifest = verify_manifest(ROOT)
    covered_roots = {entry["path"].split("/", 1)[0] for entry in manifest["files"]}

    assert covered_roots == set(manifest["export_set"])


@pytest.mark.contract_integrity
def test_integrity_check_fails_closed_on_partial_promotion(tmp_path: Path) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(snapshot / "permit-consumption")

    with pytest.raises(
        ContractsIntegrityError,
        match="missing vendored file: permit-consumption/duplicate-reservation.expected.json",
    ):
        verify_snapshot_integrity(snapshot)


@pytest.mark.contract_integrity
def test_integrity_check_fails_closed_on_stale_promotion(tmp_path: Path) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    (snapshot / "schemas" / "common" / "v1" / "definitions.schema.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        ContractsIntegrityError,
        match="digest mismatch: schemas/common/v1/definitions.schema.json",
    ):
        verify_snapshot_integrity(snapshot)


@pytest.mark.contract_integrity
def test_capability_permit_schema_closure_contains_only_its_dependencies() -> None:
    closure = schema_dependency_closure(ROOT / "schemas")

    assert closure == {
        Path("commercial/v1/capability-permit.schema.json"),
        Path("common/v1/definitions.schema.json"),
    }


@pytest.mark.contract_integrity
def test_schema_closure_assertion_rejects_an_unpromoted_reference(tmp_path: Path) -> None:
    source = tmp_path / "source"
    schemas = source / "schemas"
    capability = schemas / "commercial" / "v1" / "capability-permit.schema.json"
    definitions = schemas / "common" / "v1" / "definitions.schema.json"
    extra = schemas / "common" / "v1" / "extra.schema.json"
    capability.parent.mkdir(parents=True)
    definitions.parent.mkdir(parents=True)
    capability.write_text(
        '{"$ref":"../../common/v1/definitions.schema.json",'
        '"properties":{"extra":{"$ref":"../../common/v1/extra.schema.json"}}}',
        encoding="utf-8",
    )
    definitions.write_text("{}", encoding="utf-8")
    extra.write_text("{}", encoding="utf-8")

    with pytest.raises(
        SystemExit,
        match=rf"capability-permit schema closure has 3 members; expected {EXPECTED_SCHEMA_CLOSURE_SIZE}",
    ):
        copy_schema_closure(source, tmp_path / "exported-schemas")
