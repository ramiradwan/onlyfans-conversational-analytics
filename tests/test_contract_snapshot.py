from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools.regenerate_contract_snapshot import (
    APPROVED_SOURCE_COMMIT,
    APPROVED_SOURCE_MANIFEST_SHA256,
    APPROVED_SOURCE_REPOSITORY,
    APPROVED_SOURCE_TREE,
    EXPECTED_FILE_COUNT,
    EXPECTED_PAIRING_PROFILE,
    EXPECTED_PAIRING_VECTOR_FILES,
    EXPECTED_PROGRESS_VECTOR_FILES,
    EXPECTED_PUBLISHED_PROFILES,
    EXPORT_SET,
    EXPORT_SOURCES,
    SOURCE_MANIFEST_TARGET,
    build_records,
    selected_pairing_profile,
    selected_progress_profile,
)
from contracts.loader import (
    ContractsIntegrityError,
    load_trust_set,
    verify_manifest,
    verify_snapshot_integrity,
)


ROOT = Path(__file__).resolve().parents[1] / "contracts"
PROGRESS_PROFILE = "urn:bridge-clean:onboarding-progress:v1"
PAIRING_PROFILE = "urn:bridge-clean:companion-pairing:v1"
INSTALLATION_CLAIM_V1_PROFILE = "urn:bridge-clean:installation-claim:v1"
INSTALLATION_CLAIM_V2_PROFILE = "urn:bridge-clean:installation-claim:v2"
BOOTSTRAP_RECOVERY_V2_PROFILE = "urn:bridge-clean:bootstrap-recovery:v2"
CAPABILITY_LICENSE_PROFILE = "urn:bridge-clean:capability-license:v1"
SELECTED_PROFILES = [
    *EXPECTED_PUBLISHED_PROFILES,
    "urn:bridge-clean:capability-permit-v1",
    "urn:bridge-clean:capability-permit-consumption-policy:v1",
]
EXPECTED_APPROVED_BYTES = {
    "onboarding-progress/metadata-rejected.expected.json": "2f9029dabbc408bf53239dafce563745c9f8884f1b4ad65c98ceeda805e34c81",
    "onboarding-progress/metadata-rejected.json": "b4e6637ad87e10801094be012ba0c2e6ec382e06d6f58f900473ceb9be17358f",
    "onboarding-progress/unknown-milestone.expected.json": "2f9029dabbc408bf53239dafce563745c9f8884f1b4ad65c98ceeda805e34c81",
    "onboarding-progress/unknown-milestone.json": "691de920f7e9b24931af356d976c9d901f38bd7772020b379279b777f182d1be",
    "onboarding-progress/valid.expected.json": "dfd9af3bf11a1bf83d5ebb81873a8fb2aa1328e5471884ad4ffd07a3779b0686",
    "onboarding-progress/valid.json": "55620d8c602da8feadabdb00e28dab91cdd500a3071f165742062f79a33e6f3f",
    "schemas/commercial/v1/capability-permit.schema.json": "9b0bfee05eb11ed87876a43b6ee88035f67a6f3067254fcc79e23dd5c42b1aa8",
    "schemas/common/v1/definitions.schema.json": "c81c62d1a05a295b7aaa701a9866df707c8069f8e9910b5cbbc5e879cd8f15fa",
    "schemas/provisioning/v1/onboarding-progress-report.schema.json": "062a9f277af0c29cdc22a469d2e35d73e306fb3f6dbf6435f942558b75987d65",
    "schemas/provisioning/v1/onboarding-progress-response.schema.json": "fc3f762656fbbf605ca96ad4fa7c7401b3aa855194e952010e3b84a2ba2bf452",
    "schemas/provisioning/v1/report-proof-challenge.schema.json": "96aeefee034ee710d98013b1cb2996ba84e576ec1000e40c5b24d2b28ba5f8f8",
}


@pytest.mark.contract_integrity
def test_selected_snapshot_matches_its_independent_consumer_pin() -> None:
    manifest = verify_snapshot_integrity(ROOT)
    assert len(manifest["files"]) == EXPECTED_FILE_COUNT
    assert manifest["profiles"] == SELECTED_PROFILES
    assert manifest["export_set"] == EXPORT_SET


@pytest.mark.contract_integrity
def test_published_delivery_profiles_are_adopted_without_dropping_v1_compatibility() -> None:
    manifest, pin = build_records()

    assert selected_progress_profile() == PROGRESS_PROFILE
    assert manifest["profiles"] == SELECTED_PROFILES
    assert pin["supported_profiles"] == SELECTED_PROFILES
    assert INSTALLATION_CLAIM_V1_PROFILE in manifest["profiles"]
    assert INSTALLATION_CLAIM_V2_PROFILE in manifest["profiles"]
    assert BOOTSTRAP_RECOVERY_V2_PROFILE in manifest["profiles"]
    assert CAPABILITY_LICENSE_PROFILE in manifest["profiles"]


@pytest.mark.contract_integrity
def test_consumer_pin_names_exact_published_contract_authority() -> None:
    pin = json.loads((ROOT / "consumer-pin.json").read_text("utf-8"))

    assert pin["consumer_pin_version"] == 3
    assert pin["source_repository"] == APPROVED_SOURCE_REPOSITORY
    assert pin["source_commit"] == APPROVED_SOURCE_COMMIT
    assert pin["source_tree"] == APPROVED_SOURCE_TREE
    assert pin["source_contract_manifest_path"] == SOURCE_MANIFEST_TARGET
    assert pin["source_contract_manifest_sha256"] == APPROVED_SOURCE_MANIFEST_SHA256
    assert pin["aggregate_bundle_sha256"] == "3c4b0a774e2fe6f3bf878c6e8434bc0e2c712630e7c89951933788d70cf1ed6c"
    assert pin["contract_manifest_sha256"] == "6ba604cfa5b85cd75354fe6dd0ab55c89a0adfa005d6d161c659485cbd1d0878"
    assert {record["export"]: record["sha256"] for record in pin["conformance_manifests"]} == {
        "capability-license-v1": "c87d4ea9e70a856ab21888ddc048ebada5837713f97e30599af4c66536a2d5d1",
        "installation-claim-package-v1": "5bb58f5f2f3938a69d6381efeb38b0e4d6417aece0482ff337dafb8edb08483d",
        "bootstrap-recovery-v2": "f4205383eee5d5fcf7b6a394fdad3e3cab1a5ccb02ea63dece068a6f02e04596",
        "capability-license-hosted-api-v1": "b128955756b737ebfb58896d5fdd0b23430070817da9ab06bb970010aa125f0c",
    }


@pytest.mark.contract_integrity
def test_onboarding_progress_export_is_the_exact_selected_set() -> None:
    progress_root = ROOT / "onboarding-progress"
    actual = {
        path.relative_to(progress_root).as_posix()
        for path in progress_root.rglob("*")
        if path.is_file()
    }

    assert actual == EXPECTED_PROGRESS_VECTOR_FILES


@pytest.mark.contract_integrity
def test_companion_pairing_record_and_vectors_name_one_profile() -> None:
    pairing_root = ROOT / "companion-pairing-v1"
    actual = {
        path.relative_to(pairing_root).as_posix()
        for path in pairing_root.rglob("*")
        if path.is_file()
    }
    record = json.loads((ROOT / "companion-pairing-profile/profile.json").read_text("utf-8"))
    vectors = json.loads((pairing_root / "manifest.json").read_text("utf-8"))

    assert selected_pairing_profile() == EXPECTED_PAIRING_PROFILE
    assert actual == EXPECTED_PAIRING_VECTOR_FILES
    assert record["profile"] == vectors["profile"] == PAIRING_PROFILE
    assert vectors["test_only"] is True
    assert set(record["messages"].values()) <= {
        entry["path"] for entry in verify_manifest(ROOT)["files"]
    }


@pytest.mark.contract_integrity
def test_approved_progress_and_schema_bytes_are_exact() -> None:
    actual = {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        for relative in EXPECTED_APPROVED_BYTES
    }

    assert actual == EXPECTED_APPROVED_BYTES


@pytest.mark.contract_integrity
def test_progress_rejection_vectors_are_closed_schema_cases() -> None:
    report_schema = json.loads(
        (ROOT / "schemas/provisioning/v1/onboarding-progress-report.schema.json").read_text("utf-8")
    )
    request_schema = report_schema["properties"]["request"]
    request_properties = set(request_schema["properties"])
    milestone_values = set(request_schema["properties"]["milestone"]["enum"])
    valid = json.loads((ROOT / "onboarding-progress/valid.json").read_text("utf-8"))["request"]
    metadata = json.loads(
        (ROOT / "onboarding-progress/metadata-rejected.json").read_text("utf-8")
    )["request"]
    unknown = json.loads(
        (ROOT / "onboarding-progress/unknown-milestone.json").read_text("utf-8")
    )["request"]

    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == request_properties
    assert set(valid) == request_properties
    assert valid["milestone"] in milestone_values
    assert set(metadata) - request_properties == {"metadata"}
    assert unknown["milestone"] not in milestone_values
    assert json.loads(
        (ROOT / "onboarding-progress/metadata-rejected.expected.json").read_text("utf-8")
    ) == {"result": "schema_invalid", "valid": False}
    assert json.loads(
        (ROOT / "onboarding-progress/unknown-milestone.expected.json").read_text("utf-8")
    ) == {"result": "schema_invalid", "valid": False}


@pytest.mark.contract_integrity
def test_fixture_trust_set_is_fail_closed_outside_development() -> None:
    with pytest.raises(ContractsIntegrityError, match="not production usable"):
        load_trust_set("grant-profile-v1/keys/trust-set.json")


@pytest.mark.contract_integrity
def test_fixture_trust_set_can_be_loaded_only_in_development() -> None:
    trust_set = load_trust_set("grant-profile-v1/keys/trust-set.json", environment="development")
    assert trust_set["production_usable"] is False


@pytest.mark.contract_integrity
def test_capability_license_fixture_trust_is_not_production_trust() -> None:
    fixture = json.loads((ROOT / "capability-license-v1/trust-set.json").read_text("utf-8"))
    assert fixture["production_usable"] is False
    with pytest.raises(ContractsIntegrityError, match="not production usable"):
        load_trust_set("capability-license-v1/trust-set.json")
    assert load_trust_set(
        "capability-license-v1/trust-set.json", environment="development"
    )["production_usable"] is False


@pytest.mark.contract_integrity
def test_production_capability_license_trust_is_product_owned_and_manifest_pinned() -> None:
    relative = "production/capability-license-v1/trust-set.json"
    expected = "935cd51fe510065d6dc7d29ce481324e7ddf2be1a5431151bc74efc4f45a9173"
    manifest = verify_manifest(ROOT)
    entries = {entry["path"]: entry for entry in manifest["files"]}

    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    assert entries[relative]["sha256"] == expected
    trust_set = load_trust_set(relative)
    assert trust_set["profile"] == "urn:bridge-clean:capability-license-trust:v1"
    assert trust_set["production_usable"] is True
    assert trust_set["purpose"] == "capability-license"
    assert "production" not in EXPORT_SOURCES


@pytest.mark.contract_integrity
def test_production_grant_trust_set_is_manifest_pinned() -> None:
    trust_set = load_trust_set("production/grant-profile-v1/trust-set.json")

    assert trust_set["production_usable"] is True
    assert {entry["purpose"] for entry in trust_set["keys"]} == {
        "installation-binding",
        "membership",
        "license",
    }


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
def test_published_schema_references_resolve_inside_pinned_source_manifest() -> None:
    source_manifest = json.loads((ROOT / SOURCE_MANIFEST_TARGET).read_text("utf-8"))
    published = {entry["path"] for entry in source_manifest["files"]}
    schema_paths = {path for path in published if path.startswith("schemas/")}

    def references(value: object) -> list[str]:
        if isinstance(value, dict):
            found = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
            for child in value.values():
                found.extend(references(child))
            return found
        if isinstance(value, list):
            return [ref for child in value for ref in references(child)]
        return []

    for relative in schema_paths:
        document = json.loads((ROOT / relative).read_text("utf-8"))
        source = Path(relative)
        for ref in references(document):
            path_part = ref.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (source.parent / path_part).as_posix()
            while "/../" in resolved or resolved.startswith("../"):
                resolved = str(Path(resolved))
                break
            resolved = Path(resolved).as_posix()
            # pathlib keeps '..' lexically; resolve against the contracts root instead.
            resolved = (ROOT / source.parent / path_part).resolve().relative_to(ROOT.resolve()).as_posix()
            assert resolved in schema_paths


@pytest.mark.contract_integrity
def test_source_provenance_drift_fails_closed(tmp_path: Path) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    pin_path = snapshot / "consumer-pin.json"
    pin = json.loads(pin_path.read_text("utf-8"))
    pin["source_commit"] = "0" * 40
    pin_path.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ContractsIntegrityError, match="source provenance drifted"):
        verify_snapshot_integrity(snapshot)
