from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urljoin

import pytest

from tools.regenerate_contract_snapshot import (
    APPROVED_EXPORT_DIGESTS,
    APPROVED_PREVIOUS_INVENTORIES,
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
    _copy_exact_file,
    _tree_digest,
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

pytestmark = [pytest.mark.ci_tier('fast')]


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
    "profiles/complimentary-onboarding-activation/v1/profile.json": "b6d27ee6a1c8fa7566f55c98a1ed7651bd020c0bb1732ca5756c211c5a3dc0a3",
    "catalog/authentication-selection-admission-policy.json": "1de0add47fe202d1caa7d2dedc75bc4e5264572dd5a31f2980a9c3b8a3c00de5",
    "profiles/text-analysis-supported-profiles/v1/profile.json": "cd915cc3918743b62fd3dbf87fd76416ea99f4779b32aff95da6967d1d67994b",
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


def test_pin_migration_cannot_replace_unknown_previous_inventory_bytes(tmp_path: Path) -> None:
    previous = tmp_path / "previous.json"
    candidate = tmp_path / "candidate.json"
    previous.write_bytes(b"unexpected prior inventory\n")
    candidate.write_bytes(b"reviewed new inventory\n")
    for relative in APPROVED_PREVIOUS_INVENTORIES:
        with pytest.raises(SystemExit, match="immutable"):
            _copy_exact_file(candidate, previous, relative)
    assert previous.read_bytes() == b"unexpected prior inventory\n"


def test_pin_migration_cannot_replace_released_schema_bytes(tmp_path: Path) -> None:
    previous = tmp_path / "schema.json"
    candidate = tmp_path / "changed.json"
    previous.write_bytes(b"released bytes\n")
    candidate.write_bytes(b"new meaning\n")
    with pytest.raises(SystemExit, match="immutable"):
        _copy_exact_file(
            candidate, previous, "schemas/provisioning/v1/onboarding-progress-report.schema.json"
        )
    assert previous.read_bytes() == b"released bytes\n"


@pytest.mark.contract_integrity
def test_selected_snapshot_matches_its_independent_consumer_pin() -> None:
    manifest = verify_snapshot_integrity(ROOT)
    assert len(manifest["files"]) == EXPECTED_FILE_COUNT
    assert manifest["profiles"] == SELECTED_PROFILES
    assert manifest["export_set"] == EXPORT_SET


@pytest.mark.contract_integrity
def test_snapshot_preserves_independent_claim_and_license_authorities() -> None:
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
    assert pin["aggregate_bundle_sha256"] == "eb8afe08c008390f3c62fa95d18e20d2172b6c3622774b27836ed2c3c25ec221"
    assert pin["contract_manifest_sha256"] == "6c8eed0f70697b1fc7b3310183f0f10364c7072685b612f595956a7d5bdef748"
    assert {record["export"]: record["sha256"] for record in pin["conformance_manifests"]} == {
        "complimentary-onboarding-activation-v2": "ac40ba0edeef98847bdc63485b7df144d4b64db91abd17cc85af724df4fa85bd",
        "text-analysis-license-reissue-v1": "680455193f7ced9e6649da05dabfcf6941de876361cf7cac21511f36c356d50d",
        "text-analysis-authority-v1": "ac16c215514ed99d6c85a9189494b377a945c7a7a765896a4acbc7417d9ee50a",
        "deferred-setup-preparation-v1": "3e9885653c501b3c3f73221453f2a7c9d2b9aa81d4bf71ec6805d77c1b610a26",
        "capability-license-delivery-recovery-v1": "c086ca4cc75e589d4427150bf7f83dfa089a4c0aa3fa77bd9430c37dce685bde",
        "browser-authentication-selection-v1": "1ca8b47bd3f7067166ae6ba71fddc615bb3dad525d0e10f49db5fc0eef859e8c",
        "desktop-decision-v1": "54dabb1007de3caae82a9bea4556d0ee48f6e5053cba328f4f94d6c532ca77e2",
        "browser-ceremony-v1": "fdb51040649fb73596f47268bbca4488ed5bca6d37e2ccd9dc95657c3ffd8279",
        "initial-installation-handoff-v2": "3a60ed6d7653ec62820b98725a4a9449dff1a675708102b0a893a99af07dcd53",
        "installation-setup-continuation-v2": "8825be19fce06abf6d93f6da0d82d50ff64d04ffb24c854d901f1de214d55a7b",
        "complimentary-onboarding-activation-v1": "876d1fcebbb93291bfeefbd696260ef311d9026725e4cf6c130729018cf571f3",
        "installation-setup-continuation-v1": "748c589bcdb5bad1d432f31adab1e4ce3b02f2990cad313669a0100cc2d23632",
        "initial-installation-handoff-v1": "733d48ac8587a4491bc72e65bc2bee005a960b8fdade29e2bb5fedfb1edad258",
        "onboarding-continuity-v1": "3b1cc9a0be0a5ccbd882b9d69c5b77cb859246d7b0f1f680003eea3bc807437b",
        "capability-license-v1": "c87d4ea9e70a856ab21888ddc048ebada5837713f97e30599af4c66536a2d5d1",
        "installation-claim-package-v1": "5bb58f5f2f3938a69d6381efeb38b0e4d6417aece0482ff337dafb8edb08483d",
        "bootstrap-recovery-v2": "f4205383eee5d5fcf7b6a394fdad3e3cab1a5ccb02ea63dece068a6f02e04596",
        "capability-license-hosted-api-v1": "b128955756b737ebfb58896d5fdd0b23430070817da9ab06bb970010aa125f0c",
    }


@pytest.mark.contract_integrity
def test_installation_continuation_export_is_closed_and_manifest_bound() -> None:
    family = ROOT / "installation-setup-continuation-v1"
    manifest = json.loads((family / "manifest.json").read_text("utf-8"))
    assert manifest["profile"] == "urn:bridge-clean:installation-setup-continuation-vectors:v1"
    assert manifest["test_only"] is True
    entries = {entry["path"]: entry for entry in manifest["files"]}
    assert set(entries) == {
        "admission-cases.json", "cursor-cases.json", "lifecycle-cases.json",
        "parsing-cases.json", "proof-cases.json", "recovery-cases.json",
        "response-cases.json", "schema-cases.json",
    }
    assert {path.name for path in family.iterdir()} == {*entries, "manifest.json"}
    case_count = 0
    for relative, entry in entries.items():
        data = (family / relative).read_bytes()
        assert entry == {
            "path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        case_count += len(json.loads(data))
    assert case_count == manifest["case_count"]

    snapshot = verify_snapshot_integrity(ROOT)
    paths = {entry["path"] for entry in snapshot["files"]}
    for name in ("installation-setup-continuation", "installation-setup-continuation-proof"):
        profile = json.loads((ROOT / f"profiles/{name}/v1/profile.json").read_text("utf-8"))
        assert profile["profile"] in snapshot["profiles"]
        assert set(profile["messages"].values()) <= paths
    assert "openapi/installation-setup-continuation-v1.openapi.yaml" in paths


@pytest.mark.contract_integrity
@pytest.mark.parametrize("family, profile_paths", [
    ("initial-installation-handoff-v2", (
        "profiles/initial-installation-handoff-v2/profile.json",
        "profiles/initial-installation-handoff-proof-v2/profile.json",
    )),
    ("installation-setup-continuation-v2", (
        "profiles/installation-setup-continuation-v2/profile.json",
        "profiles/installation-setup-continuation-proof-v2/profile.json",
    )),
    ("complimentary-onboarding-activation-v1", (
        "profiles/complimentary-onboarding-activation/v1/profile.json",
    )),
    ("desktop-decision-v1", (
        "profiles/desktop-decision/v1/profile.json",
        "profiles/desktop-decision-proof/v1/profile.json",
    )),
    ("browser-authentication-selection-v1", (
        "profiles/browser-authentication-selection/v1/profile.json",
        "profiles/browser-authentication-selection-proof/v1/profile.json",
    )),
    ("browser-ceremony-v1", (
        "profiles/browser-ceremony/v1/profile.json",
        "profiles/browser-ceremony-proof/v1/profile.json",
    )),
    ("capability-license-delivery-recovery-v1", (
        "profiles/capability-license-delivery-recovery/v1/profile.json",
        "profiles/capability-license-delivery-recovery-proof/v1/profile.json",
    )),
])
def test_selected_exports_are_closed_and_manifest_bound(
    family: str, profile_paths: tuple[str, ...],
) -> None:
    vector_root = ROOT / family
    manifest = json.loads((vector_root / "manifest.json").read_bytes())
    stem, version = family.rsplit("-v", 1)
    assert manifest["profile"] == f"urn:bridge-clean:{stem}-vectors:v{version}"
    assert manifest["test_only"] is True
    entries = {entry["path"]: entry for entry in manifest["files"]}
    assert {path.name for path in vector_root.iterdir()} == {*entries, "manifest.json"}
    count = 0
    for relative, entry in entries.items():
        data = (vector_root / relative).read_bytes()
        assert entry == {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        count += len(json.loads(data))
    assert count == manifest["case_count"]
    snapshot = verify_snapshot_integrity(ROOT)
    paths = {entry["path"] for entry in snapshot["files"]}
    for relative in profile_paths:
        profile = json.loads((ROOT / relative).read_bytes())
        assert profile["profile"] in snapshot["profiles"]
        assert set(profile["messages"].values()) <= paths
    assert f"openapi/{family}.openapi.yaml" in paths


@pytest.mark.contract_integrity
def test_admission_policy_does_not_reenable_archived_onboarding_profiles() -> None:
    snapshot = verify_snapshot_integrity(ROOT)
    policy_path = "catalog/authentication-selection-admission-policy.json"
    assert policy_path in {entry["path"] for entry in snapshot["files"]}
    policy = json.loads((ROOT / policy_path).read_bytes())
    assert policy["authentication_correlation_profiles"] == [
        "urn:bridge-clean:browser-ceremony:v1",
        "urn:bridge-clean:browser-ceremony-proof:v1",
    ]
    retired = {
        f"urn:bridge-clean:{name}:v1"
        for name in (
            "initial-installation-handoff", "initial-installation-handoff-proof",
            "installation-setup-continuation", "installation-setup-continuation-proof",
        )
    }
    assert set(policy["retired_admission_profiles"]) == retired
    assert policy["authentication_selection_profiles"] == [
        "urn:bridge-clean:browser-authentication-selection:v1",
        "urn:bridge-clean:browser-authentication-selection-proof:v1",
    ]
    assert policy["browser_consent_actions"] == []
    admitted = set(
        policy["authentication_correlation_profiles"] + policy["retained_domain_profiles"]
        + policy["desktop_decision_profiles"] + policy["authentication_selection_profiles"]
    )
    assert retired.isdisjoint(admitted)
    assert admitted <= set(snapshot["profiles"])
    assert policy["legacy_adapters_required"] is False
    assert policy["archived_verification_material_authorizes_service"] is False


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

    documents = {relative: json.loads((ROOT / relative).read_bytes())
                 for relative in schema_paths}
    identifiers = {document["$id"]: relative for relative, document in documents.items()}
    assert len(identifiers) == len(documents)
    for relative, document in documents.items():
        for ref in references(document):
            resolved = urljoin(document["$id"], ref).split("#", 1)[0]
            assert resolved in identifiers, (relative, ref)



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


@pytest.mark.contract_integrity
def test_deferred_preparation_policy_and_supplemental_export_are_closed() -> None:
    policy = json.loads((ROOT / "catalog/deferred-setup-preparation-policy-v1.json").read_bytes())
    assert policy["wire_profiles_unchanged"] is True
    assert policy["new_public_operations"] == []
    assert policy["new_proof_purposes"] == []
    assert policy["legacy_adapters_required"] is False
    assert policy["product"]["automatic_replacement_or_id_reuse"] is False
    assert policy["product"]["explicit_exact_offer_confirmation_required"] is True
    family = ROOT / "deferred-setup-preparation-v1"
    manifest = json.loads((family / "manifest.json").read_bytes())
    assert manifest["policy"] == policy["policy"]
    assert manifest["wire_profiles_unchanged"] is True
    assert manifest["test_only"] is True
    entries = {entry["path"]: entry for entry in manifest["files"]}
    assert set(entries) == {
        "admission-cases.json", "classification-cases.json",
        "lifecycle-cases.json", "scheduling-cases.json",
    }
    assert {path.name for path in family.iterdir()} == {*entries, "manifest.json"}
    case_count = 0
    for relative, entry in entries.items():
        data = (family / relative).read_bytes()
        assert entry == {
            "path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }
        case_count += len(json.loads(data))
    assert case_count == manifest["case_count"] == 147


@pytest.mark.contract_integrity
def test_text_analysis_export_has_exact_independent_pins_and_inventory() -> None:
    family = "text-analysis-authority-v1"
    root = ROOT / family
    vector = json.loads((root / "manifest.json").read_bytes())
    pin = json.loads((ROOT / "consumer-pin.json").read_bytes())
    expected_vector = {
        "export": family, "path": f"{family}/manifest.json",
        "sha256": "ac16c215514ed99d6c85a9189494b377a945c7a7a765896a4acbc7417d9ee50a",
    }
    expected_trust = {
        "export": family, "path": f"{family}/trust-set.json",
        "sha256": "c1020fc2fb8faf2e42d3a3e204452a420ac4c0badc37a22f9217f37f82f8ab7b",
    }
    assert expected_vector in pin["vector_manifests"]
    assert expected_vector in pin["conformance_manifests"]
    assert expected_trust in pin["trust_sets"]
    assert {"export": family, "version": "1.0.1"} in pin["generator_versions"]
    assert vector["profile"] == "urn:bridge-clean:text-analysis-authority-vectors:v1"
    assert vector["production_usable"] is False
    assert vector["generator_version"] == "1.0.1"
    entries = {entry["path"]: entry for entry in vector["files"]}
    assert set(entries) == {
        "schema-cases.json", "signed-cases.json", "proof-cases.json",
        "admission-cases.json", "lifecycle-cases.json", "trust-set.json",
    }
    assert {path.name for path in root.iterdir()} == {*entries, "manifest.json"}
    case_counts = {}
    for relative, entry in entries.items():
        data = (root / relative).read_bytes()
        assert entry == {"path": relative, "size": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()}
        if relative.endswith("-cases.json"):
            case_counts[relative] = len(json.loads(data))
    assert case_counts == {"schema-cases.json": 1887, "signed-cases.json": 230,
                           "proof-cases.json": 156, "admission-cases.json": 34,
                           "lifecycle-cases.json": 21}
    assert sum(case_counts.values()) == vector["case_count"] == 2328
    supported = json.loads((ROOT / "profiles/text-analysis-supported-profiles/v1/profile.json").read_bytes())
    assert len(supported["profiles"]) == 12
    assert set(supported["profiles"]) < set(pin["supported_profiles"])
    assert vector["profile"] not in pin["supported_profiles"]
    assert "urn:bridge-clean:capability-permit-purchase:v2" in pin["supported_profiles"]
    assert not ({"capability-permit-purchase-v1", "capability-purchase-session-v1"} & set(pin["export_set"]))


@pytest.mark.contract_integrity
def test_text_analysis_fixture_trust_remains_nonproduction() -> None:
    relative = "text-analysis-authority-v1/trust-set.json"
    with pytest.raises(ContractsIntegrityError, match="not production usable"):
        load_trust_set(relative)
    trust = load_trust_set(relative, environment="development")
    assert trust["production_usable"] is False
    assert {key["purpose"] for key in trust["keys"]} == {
        "text-analysis-license", "text-analysis-preview",
    }
    assert all(key["fixture_only"] is True for key in trust["keys"])


@pytest.mark.contract_integrity
def test_selected_text_analysis_pins_cannot_be_omitted(tmp_path: Path) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    pin_path = snapshot / "consumer-pin.json"
    pin = json.loads(pin_path.read_bytes())
    for collection in ("vector_manifests", "conformance_manifests", "trust_sets", "generator_versions"):
        pin[collection] = [entry for entry in pin[collection]
                           if entry["export"] != "text-analysis-authority-v1"]
    pin_path.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ContractsIntegrityError, match="text-analysis authority pins are missing"):
        verify_snapshot_integrity(snapshot)


@pytest.mark.contract_integrity
@pytest.mark.parametrize("export", EXPORT_SOURCES)
def test_selected_fixture_exports_retain_exact_approved_bytes(export: str) -> None:
    assert _tree_digest(ROOT / export) == APPROVED_EXPORT_DIGESTS[export]


@pytest.mark.contract_integrity
@pytest.mark.parametrize("family, expected_hash, expected_count, expected_files", [
    ("complimentary-onboarding-activation-v2",
     "ac40ba0edeef98847bdc63485b7df144d4b64db91abd17cc85af724df4fa85bd",
     311, {"policy-cases.json", "schema-cases.json"}),
    ("text-analysis-license-reissue-v1",
     "680455193f7ced9e6649da05dabfcf6941de876361cf7cac21511f36c356d50d",
     1022, {"admission-cases.json", "lifecycle-cases.json", "proof-cases.json",
            "response-cases.json", "schema-cases.json", "trust-set.json"}),
])
def test_additive_family_inventory_has_independent_exact_pins(
    family: str, expected_hash: str, expected_count: int, expected_files: set[str],
) -> None:
    pin = json.loads((ROOT / "consumer-pin.json").read_bytes())
    root = ROOT / family
    manifest = json.loads((root / "manifest.json").read_bytes())
    record = {"export": family, "path": f"{family}/manifest.json", "sha256": expected_hash}
    assert hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest() == expected_hash
    assert record in pin["vector_manifests"]
    assert record in pin["conformance_manifests"]
    assert {"export": family, "version": "1.0.0"} in pin["generator_versions"]
    assert manifest["production_usable"] is False
    assert manifest["profile"] not in pin["supported_profiles"]
    entries = {entry["path"]: entry for entry in manifest["files"]}
    assert set(entries) == expected_files
    assert {path.name for path in root.iterdir()} == {*expected_files, "manifest.json"}
    count = 0
    for relative, entry in entries.items():
        data = (root / relative).read_bytes()
        assert entry == {"path": relative, "size": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()}
        if relative.endswith("-cases.json"):
            count += len(json.loads(data))
    assert count == manifest["case_count"] == expected_count
    source = json.loads((ROOT / SOURCE_MANIFEST_TARGET).read_bytes())
    published = {entry["path"]: entry["sha256"] for entry in source["files"]}
    for entry in manifest["inputs"]:
        if entry["path"] in published:
            assert entry["sha256"] == published[entry["path"]]


@pytest.mark.contract_integrity
def test_reissue_inventory_does_not_widen_initial_issuance_support_or_trust() -> None:
    pin = json.loads((ROOT / "consumer-pin.json").read_bytes())
    support = json.loads((ROOT / "profiles/license-reissue-text-analysis-supported-profiles/v1/profile.json").read_bytes())
    profiles = support["profiles"]
    assert profiles == sorted(profiles) == [
        "urn:bridge-clean:text-analysis-license-reissue-authorization:v1",
        "urn:bridge-clean:text-analysis-license-reissue-proof:v1",
        "urn:bridge-clean:text-analysis-license-reissue-supported-profiles:v1",
        "urn:bridge-clean:text-analysis-license-reissue:v1",
        "urn:bridge-clean:text-analysis-license:v1",
        "urn:bridge-clean:text-analysis-release-identity:v1",
    ]
    from app.security.text_analysis_contract import SUPPORTED
    original = json.loads((ROOT / "profiles/text-analysis-supported-profiles/v1/profile.json").read_bytes())
    assert tuple(original["profiles"]) == SUPPORTED
    assert len(SUPPORTED) == 12
    assert set(profiles[:4]).isdisjoint(SUPPORTED)
    assert {"export": "text-analysis-license-reissue-v1",
            "path": "text-analysis-license-reissue-v1/trust-set.json",
            "sha256": "689235cbef993628fe8e8921533f264f199c72020eb11603b1ec71199b58397b"} in pin["trust_sets"]
    assert "complimentary-onboarding-activation-v2" not in {
        entry["export"] for entry in pin["trust_sets"]}
    relative = "text-analysis-license-reissue-v1/trust-set.json"
    with pytest.raises(ContractsIntegrityError, match="not production usable"):
        load_trust_set(relative)
    trust = load_trust_set(relative, environment="development")
    assert trust["production_usable"] is False
    assert {key["purpose"] for key in trust["keys"]} == {"text-analysis-license"}
    assert all(key["fixture_only"] is True for key in trust["keys"])


@pytest.mark.contract_integrity
@pytest.mark.parametrize("family", [
    "complimentary-onboarding-activation-v2", "text-analysis-license-reissue-v1",
])
@pytest.mark.parametrize("mutation", [
    "omit_family", "omit_conformance", "conflate_conformance", "wrong_generator",
])
def test_additive_family_pin_tampering_fails_closed(
    tmp_path: Path, family: str, mutation: str,
) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    pin_path = snapshot / "consumer-pin.json"
    pin = json.loads(pin_path.read_bytes())
    if mutation == "omit_family":
        for collection in ("vector_manifests", "conformance_manifests",
                           "trust_sets", "generator_versions"):
            pin[collection] = [entry for entry in pin[collection]
                               if entry["export"] != family]
    elif mutation == "omit_conformance":
        pin["conformance_manifests"] = [entry for entry in pin["conformance_manifests"]
                                        if entry["export"] != family]
    elif mutation == "conflate_conformance":
        foreign = next(entry for entry in pin["conformance_manifests"]
                       if entry["export"] == "text-analysis-authority-v1")
        target = next(entry for entry in pin["conformance_manifests"]
                      if entry["export"] == family)
        target.update(path=foreign["path"], sha256=foreign["sha256"])
    else:
        target = next(entry for entry in pin["generator_versions"]
                      if entry["export"] == family)
        target["version"] = "1.0.1"
    pin_path.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ContractsIntegrityError):
        verify_snapshot_integrity(snapshot)


@pytest.mark.contract_integrity
@pytest.mark.parametrize("mutation", ["omit_reissue_trust", "conflate_reissue_trust", "starter_trust"])
def test_additive_family_trust_pin_tampering_fails_closed(
    tmp_path: Path, mutation: str,
) -> None:
    snapshot = tmp_path / "contracts"
    shutil.copytree(ROOT, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    pin_path = snapshot / "consumer-pin.json"
    pin = json.loads(pin_path.read_bytes())
    family = "text-analysis-license-reissue-v1"
    if mutation == "omit_reissue_trust":
        pin["trust_sets"] = [entry for entry in pin["trust_sets"]
                             if entry["export"] != family]
    else:
        foreign = next(entry for entry in pin["trust_sets"]
                       if entry["export"] == "text-analysis-authority-v1")
        if mutation == "conflate_reissue_trust":
            target = next(entry for entry in pin["trust_sets"]
                          if entry["export"] == family)
            target.update(path=foreign["path"], sha256=foreign["sha256"])
        else:
            pin["trust_sets"].append({**foreign, "export": "complimentary-onboarding-activation-v2"})
    pin_path.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ContractsIntegrityError):
        verify_snapshot_integrity(snapshot)
