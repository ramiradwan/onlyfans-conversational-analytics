from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools.regenerate_contract_snapshot import (
    EXPECTED_PROGRESS_VECTOR_FILES,
    EXPECTED_SCHEMA_CLOSURE,
    build_records,
    copy_schema_closure,
    schema_dependency_closure,
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
INSTALLATION_CLAIM_PROFILE = "urn:bridge-clean:installation-claim:v1"
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
    assert len(manifest["files"]) == 437
    assert manifest["profiles"] == [
        "urn:bridge-clean:grant-profile:v1",
        "urn:bridge-clean:capability-permit-v1",
        "urn:bridge-clean:capability-permit-consumption-policy:v1",
        PROGRESS_PROFILE,
    ]
    assert manifest["export_set"] == [
        "grant-profile-v1",
        "capability-permit-v1",
        "permit-consumption",
        "production",
        "schemas",
        "onboarding-progress",
    ]


@pytest.mark.contract_integrity
def test_onboarding_progress_is_an_independently_supported_profile_without_t2() -> None:
    manifest, pin = build_records()
    expected = [
        "urn:bridge-clean:grant-profile:v1",
        "urn:bridge-clean:capability-permit-v1",
        "urn:bridge-clean:capability-permit-consumption-policy:v1",
        PROGRESS_PROFILE,
    ]

    assert selected_progress_profile() == PROGRESS_PROFILE
    assert manifest["profiles"] == expected
    assert pin["supported_profiles"] == expected
    assert INSTALLATION_CLAIM_PROFILE not in manifest["profiles"]


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
def test_selected_schema_closure_contains_only_deliberate_dependencies() -> None:
    closure = schema_dependency_closure(ROOT / "schemas")

    assert closure == EXPECTED_SCHEMA_CLOSURE


@pytest.mark.contract_integrity
def test_schema_closure_assertion_rejects_an_unpromoted_reference(tmp_path: Path) -> None:
    source = tmp_path / "source"
    schemas = source / "schemas"
    capability = schemas / "commercial" / "v1" / "capability-permit.schema.json"
    extra = schemas / "common" / "v1" / "extra.schema.json"
    for relative in EXPECTED_SCHEMA_CLOSURE:
        schema = schemas / relative
        schema.parent.mkdir(parents=True, exist_ok=True)
        schema.write_text("{}", encoding="utf-8")
    capability.write_text(
        '{"$ref":"../../common/v1/definitions.schema.json",'
        '"properties":{"extra":{"$ref":"../../common/v1/extra.schema.json"}}}',
        encoding="utf-8",
    )
    extra.write_text("{}", encoding="utf-8")

    with pytest.raises(
        SystemExit,
        match=r"selected schema closure does not match approved closure: common/v1/extra.schema.json",
    ):
        copy_schema_closure(source, tmp_path / "exported-schemas")
