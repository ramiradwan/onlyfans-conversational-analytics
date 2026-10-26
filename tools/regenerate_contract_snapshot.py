#!/usr/bin/env python3
"""Regenerate the selected public contract export and its consumer pin."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
APPROVED_SOURCE_REPOSITORY = "ramiradwan/creator-platform-contracts"
APPROVED_SOURCE_COMMIT = "5c1d9b58f56258c905ccfc803806cb271f4e721a"
APPROVED_SOURCE_TREE = "f1348abf33d3d217717a6bffc16093cc9dfb240e"
APPROVED_SOURCE_MANIFEST_SHA256 = "fdec45fabe3b657d33bbf4c6b05133eee06952be5d4669b4914fcf9135fedb39"
SOURCE_MANIFEST = "contract-manifest.json"
SOURCE_MANIFEST_EXPORT = "source-contract-manifest"
SOURCE_MANIFEST_TARGET = f"{SOURCE_MANIFEST_EXPORT}/contract-manifest.json"
PUBLISHED_ROOTS = ("catalog", "openapi", "profiles", "schemas")

# Only these additive inventories may replace reviewed previous bytes during
# this pin migration. All released schemas, profiles and fixtures remain immutable.
APPROVED_PREVIOUS_INVENTORIES = {
    "catalog/contracts.yaml": "2cb8b3de4b36576c9b6f1d18720e2b184ee7e13c43b93c37ee4839c2b5e8ca4a",
    "contract-manifest.json": "3391ce59f60d6162df72872965e16facb22ee6230efb27bdcb618ddb6f616477"
}

EXPORT_SET = [
    "grant-profile-v1",
    "capability-permit-v1",
    "permit-consumption",
    "production",
    "schemas",
    "onboarding-progress",
    "companion-pairing-v1",
    "companion-pairing-profile",
    "profiles",
    "openapi",
    "catalog",
    "capability-license-v1",
    "installation-claim-v1",
    "installation-claim-package-v1",
    "installation-key-proof-v1",
    "bootstrap-recovery-v2",
    "capability-license-hosted-api-v1",
    "onboarding-continuity-v1",
    "initial-installation-handoff-v1",
    "installation-setup-continuation-v1",
    "initial-installation-handoff-v2",
    "installation-setup-continuation-v2",
    "complimentary-onboarding-activation-v1",
    "browser-ceremony-v1",
    "desktop-decision-v1",
    "browser-authentication-selection-v1",
    "capability-license-delivery-recovery-v1",
    "deferred-setup-preparation-v1",
    "source-contract-manifest",
    "text-analysis-authority-v1",
    "complimentary-onboarding-activation-v2",
    "text-analysis-license-reissue-v1",
]

EXPORT_SOURCES = {
    "installation-setup-continuation-v1": "test-vectors/installation-setup-continuation-v1",
    "initial-installation-handoff-v1": "test-vectors/initial-installation-handoff-v1",
    "onboarding-continuity-v1": "test-vectors/onboarding-continuity-v1",
    "grant-profile-v1": "test-vectors/grant-profile-v1",
    "capability-permit-v1": "test-vectors/capability-permit-v1",
    "permit-consumption": "test-vectors/adr-0012-v1/permit-consumption",
    "onboarding-progress": "test-vectors/adr-0012-v1/onboarding-progress",
    "companion-pairing-v1": "test-vectors/companion-pairing-v1",
    "companion-pairing-profile": "profiles/companion-pairing/v1",
    "capability-license-v1": "test-vectors/capability-license-v1",
    "installation-claim-v1": "test-vectors/installation-claim-v1",
    "installation-claim-package-v1": "test-vectors/installation-claim-package-v1",
    "installation-key-proof-v1": "test-vectors/installation-key-proof-v1",
    "bootstrap-recovery-v2": "api-vectors/bootstrap-recovery-v2",
    "capability-license-hosted-api-v1": "api-vectors/capability-license-hosted-api-v1",
    "initial-installation-handoff-v2": "test-vectors/initial-installation-handoff-v2",
    "installation-setup-continuation-v2": "test-vectors/installation-setup-continuation-v2",
    "complimentary-onboarding-activation-v1": "test-vectors/complimentary-onboarding-activation-v1",
    "browser-ceremony-v1": "test-vectors/browser-ceremony-v1",
    "desktop-decision-v1": "test-vectors/desktop-decision-v1",
    "browser-authentication-selection-v1": "test-vectors/browser-authentication-selection-v1",
    "capability-license-delivery-recovery-v1": "test-vectors/capability-license-delivery-recovery-v1",
    "deferred-setup-preparation-v1": "test-vectors/deferred-setup-preparation-v1",
    "text-analysis-authority-v1": "test-vectors/text-analysis-authority-v1",
    "complimentary-onboarding-activation-v2": "test-vectors/complimentary-onboarding-activation-v2",
    "text-analysis-license-reissue-v1": "test-vectors/text-analysis-license-reissue-v1",
}

APPROVED_EXPORT_DIGESTS = {
    "installation-setup-continuation-v1": "63b47522da7a855e4e01b3e2bdbac7d39f9ec99af32780a64dbcc1fbc1a97b5d",
    "initial-installation-handoff-v1": "2b8522ba09c6b57629af52a312f9b424d74d9276add8b36cdbc850250df3c48b",
    "onboarding-continuity-v1": "98569965245348223b3fefa97ceb21744628090f5a37436f73bac5a7b91256ba",
    "grant-profile-v1": "5059ee95f0c847f7a33a787c80066e84359a2d4e0d2e8774637bfc387bece09b",
    "capability-permit-v1": "6a344d2ea66ff5af8279f6b057335829f61cd4cf8b1432c2f3bc122d4e32dae7",
    "permit-consumption": "e5e8646a5dc0a7d51f9223ccd8ee70c66c8ba2b1ba8ff04ac40be314dc945697",
    "onboarding-progress": "3262eeebd807930fa316389154a81a296ae4c87c216de032a0e59ee210854a75",
    "companion-pairing-v1": "86316c523fe0afd452995fc64e355e3a42d0bc096f8d970edb7b4fb66155fd03",
    "companion-pairing-profile": "830f46a14f6d59018b649aa4b7827fa35aafdf82433f1d6692b8ef42e3b940bc",
    "capability-license-v1": "b6e2ea754b58f4ca73b91efc085f2f9ddec87e75ab5422a126be0a8b73cba3f2",
    "installation-claim-v1": "770fd00b7395fb99cb315791e114ba8f6a1895013379de641dbb9dcf0f678c49",
    "installation-claim-package-v1": "04f39c9f478db77008fb311a90c8490e2c7528cff00b3cb343199f49e9b3af61",
    "installation-key-proof-v1": "19b9b5bfcb412086c5a03e4e6d776111420a404bba81924eeb8d0fd91bfcf799",
    "bootstrap-recovery-v2": "96d3adb571cc3f90f93748203f601dbdaf6fad95c2a7c891ecaa3ebbfcbe81d1",
    "capability-license-hosted-api-v1": "0ef82e619681e9beb7d0842a5a948171e3e5057ac81f498af38cf20d69a5731e",
    "initial-installation-handoff-v2": "95682bf35a5133d0bc6edd778133e8c31388a8917589b69a021413f4feac9c08",
    "installation-setup-continuation-v2": "9673deaa3c38aba0832a68e2e290720447f8c566c8f0e12f5c5875e67c29ea97",
    "complimentary-onboarding-activation-v1": "b9eb0b9a68dec64a67ce2522b641d5eddf9ef5a86e5cc67a459a66af9fd37cba",
    "browser-ceremony-v1": "824a953fbee1b5433402ca2f73f2f4ca3a458ba4ea038fc4c7db688c9e761c47",
    "desktop-decision-v1": "7fa1cdd451ac4acb570c11a6e3e440ba4e00bc8481d28c4aad22d706534b7efa",
    "browser-authentication-selection-v1": "d99a5bb63f45c95a2ef36b9398fb1c9130a8269635722499e535228c0043a29f",
    "capability-license-delivery-recovery-v1": "b20f15cb6539b9345ac51088427176e52e6a7d7850c0976aa98a543cc4700906",
    "deferred-setup-preparation-v1": "7c32147ecc17a04d673e06416f8d889748e128c3bc2386f19fb5870de6c33e39",
    "text-analysis-authority-v1": "985d40ba015958bdafcb18ffa13db66596782dc69849923b1fc79fdc6a18b365",
    "complimentary-onboarding-activation-v2": "2d29456e4d6da30eddf55577968f09ddde8ff80caabf600560236557ebc5b4ad",
    "text-analysis-license-reissue-v1": "4734e2f6c892c9c9a398c813c74e58925bc728898360b8a10164cc87a521d63c",
}

EXPECTED_PROGRESS_VECTOR_FILES = frozenset(
    {
        "metadata-rejected.expected.json",
        "metadata-rejected.json",
        "unknown-milestone.expected.json",
        "unknown-milestone.json",
        "valid.expected.json",
        "valid.json",
    }
)
EXPECTED_PROGRESS_PROFILE = "urn:bridge-clean:onboarding-progress:v1"
EXPECTED_PAIRING_PROFILE = "urn:bridge-clean:companion-pairing:v1"
EXPECTED_PAIRING_PROFILE_FILES = frozenset({"profile.json"})
EXPECTED_PAIRING_VECTOR_FILES = frozenset(
    {
        "authorization-cases.json",
        "confirm-cases.json",
        "manifest.json",
        "offer-cases.json",
        "request-cases.json",
        "trust-set.json",
        "vector.json",
    }
)
EXPECTED_FILE_COUNT = 1159
EXPECTED_PUBLISHED_FILE_COUNT = 349
EXPECTED_PUBLISHED_PROFILES = (
    "urn:bridge-clean:bootstrap-recovery:v1",
    "urn:bridge-clean:bootstrap-recovery:v2",
    "urn:bridge-clean:browser-authentication-selection-proof:v1",
    "urn:bridge-clean:browser-authentication-selection:v1",
    "urn:bridge-clean:browser-ceremony-proof:v1",
    "urn:bridge-clean:browser-ceremony:v1",
    "urn:bridge-clean:capability-license-activation-package:v1",
    "urn:bridge-clean:capability-license-activation-proof:v1",
    "urn:bridge-clean:capability-license-activation:v1",
    "urn:bridge-clean:capability-license-delivery-recovery-proof:v1",
    "urn:bridge-clean:capability-license-delivery-recovery:v1",
    "urn:bridge-clean:capability-license-exchange-quote:v1",
    "urn:bridge-clean:capability-license-exchange:v1",
    "urn:bridge-clean:capability-license-redemption-continuation:v1",
    "urn:bridge-clean:capability-license-redemption-proof:v1",
    "urn:bridge-clean:capability-license-redemption:v1",
    "urn:bridge-clean:capability-license-reissue-authorization:v1",
    "urn:bridge-clean:capability-license-reissue-package:v1",
    "urn:bridge-clean:capability-license-reissue:v1",
    "urn:bridge-clean:capability-license:v1",
    "urn:bridge-clean:capability-permit-purchase-proof:v1",
    "urn:bridge-clean:capability-permit-purchase-proof:v2",
    "urn:bridge-clean:capability-permit-purchase:v1",
    "urn:bridge-clean:capability-permit-purchase:v2",
    "urn:bridge-clean:capability-permit:v1",
    "urn:bridge-clean:capability-purchase-session-proof:v1",
    "urn:bridge-clean:capability-purchase-session:v1",
    "urn:bridge-clean:companion-pairing:v1",
    "urn:bridge-clean:complimentary-onboarding-activation:v1",
    "urn:bridge-clean:complimentary-onboarding-activation:v2",
    "urn:bridge-clean:creator-association:v1",
    "urn:bridge-clean:desktop-decision-proof:v1",
    "urn:bridge-clean:desktop-decision:v1",
    "urn:bridge-clean:grant-profile:v1",
    "urn:bridge-clean:hosted-onboarding:v1",
    "urn:bridge-clean:initial-installation-handoff-proof:v1",
    "urn:bridge-clean:initial-installation-handoff-proof:v2",
    "urn:bridge-clean:initial-installation-handoff:v1",
    "urn:bridge-clean:initial-installation-handoff:v2",
    "urn:bridge-clean:installation-claim-package:v1",
    "urn:bridge-clean:installation-claim-package:v2",
    "urn:bridge-clean:installation-claim:v1",
    "urn:bridge-clean:installation-claim:v2",
    "urn:bridge-clean:installation-setup-continuation-proof:v1",
    "urn:bridge-clean:installation-setup-continuation-proof:v2",
    "urn:bridge-clean:installation-setup-continuation:v1",
    "urn:bridge-clean:installation-setup-continuation:v2",
    "urn:bridge-clean:onboarding-continuation:v1",
    "urn:bridge-clean:onboarding-progress:v1",
    "urn:bridge-clean:onboarding-proof:v1",
    "urn:bridge-clean:onboarding-transfer:v1",
    "urn:bridge-clean:provisioning-proof:v1",
    "urn:bridge-clean:release-descriptor:v1",
    "urn:bridge-clean:text-analysis-authority-delivery-proof:v1",
    "urn:bridge-clean:text-analysis-authority-delivery:v1",
    "urn:bridge-clean:text-analysis-authority-issuance:v1",
    "urn:bridge-clean:text-analysis-license-issuance-proof:v1",
    "urn:bridge-clean:text-analysis-license-reissue-authorization:v1",
    "urn:bridge-clean:text-analysis-license-reissue-proof:v1",
    "urn:bridge-clean:text-analysis-license-reissue-supported-profiles:v1",
    "urn:bridge-clean:text-analysis-license-reissue:v1",
    "urn:bridge-clean:text-analysis-license:v1",
    "urn:bridge-clean:text-analysis-preview-acquisition-proof:v1",
    "urn:bridge-clean:text-analysis-preview-acquisition:v1",
    "urn:bridge-clean:text-analysis-preview-issuance-proof:v1",
    "urn:bridge-clean:text-analysis-preview-output-policy:v1",
    "urn:bridge-clean:text-analysis-preview:v1",
    "urn:bridge-clean:text-analysis-release-identity:v1",
    "urn:bridge-clean:text-analysis-supported-profiles:v1",
)
LEGACY_VECTOR_PROFILE_ALIASES = ("urn:bridge-clean:capability-permit-v1",)
POLICY_PROFILES = {"permit-consumption": "permit-consumption/policy.json"}
VECTOR_MANIFESTS = {
    "grant-profile-v1": "grant-profile-v1/manifest.json",
    "capability-permit-v1": "capability-permit-v1/manifest.json",
    "companion-pairing-v1": "companion-pairing-v1/manifest.json",
    "text-analysis-authority-v1": "text-analysis-authority-v1/manifest.json",
    "complimentary-onboarding-activation-v2": "complimentary-onboarding-activation-v2/manifest.json",
    "text-analysis-license-reissue-v1": "text-analysis-license-reissue-v1/manifest.json",
}
TRUST_SETS = {
    "grant-profile-v1": "grant-profile-v1/keys/trust-set.json",
    "capability-permit-v1": "capability-permit-v1/trust-set.json",
    "companion-pairing-v1": "companion-pairing-v1/trust-set.json",
    "text-analysis-authority-v1": "text-analysis-authority-v1/trust-set.json",
    "text-analysis-license-reissue-v1": "text-analysis-license-reissue-v1/trust-set.json",
}
CONFORMANCE_MANIFESTS = {
    "installation-setup-continuation-v1": "installation-setup-continuation-v1/manifest.json",
    "initial-installation-handoff-v1": "initial-installation-handoff-v1/manifest.json",
    "onboarding-continuity-v1": "onboarding-continuity-v1/manifest.json",
    "capability-license-v1": "capability-license-v1/manifest.json",
    "installation-claim-package-v1": "installation-claim-package-v1/manifest.json",
    "bootstrap-recovery-v2": "bootstrap-recovery-v2/manifest.json",
    "capability-license-hosted-api-v1": "capability-license-hosted-api-v1/manifest.json",
    "initial-installation-handoff-v2": "initial-installation-handoff-v2/manifest.json",
    "installation-setup-continuation-v2": "installation-setup-continuation-v2/manifest.json",
    "complimentary-onboarding-activation-v1": "complimentary-onboarding-activation-v1/manifest.json",
    "browser-ceremony-v1": "browser-ceremony-v1/manifest.json",
    "desktop-decision-v1": "desktop-decision-v1/manifest.json",
    "browser-authentication-selection-v1": "browser-authentication-selection-v1/manifest.json",
    "capability-license-delivery-recovery-v1": "capability-license-delivery-recovery-v1/manifest.json",
    "deferred-setup-preparation-v1": "deferred-setup-preparation-v1/manifest.json",
    "text-analysis-authority-v1": "text-analysis-authority-v1/manifest.json",
    "complimentary-onboarding-activation-v2": "complimentary-onboarding-activation-v2/manifest.json",
    "text-analysis-license-reissue-v1": "text-analysis-license-reissue-v1/manifest.json",
}
PAIRING_PROFILE_RECORD = "companion-pairing-profile/profile.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encode(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _tree_digest(root: Path) -> str:
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    material = b"".join(
        path.relative_to(root).as_posix().encode("utf-8")
        + b"\0"
        + sha256(path).encode("ascii")
        + b"\n"
        for path in files
    )
    return hashlib.sha256(material).hexdigest()


def fixture_entries() -> list[dict[str, Any]]:
    files = sorted(
        (
            path
            for export in EXPORT_SET
            for path in (CONTRACTS_ROOT / export).rglob("*")
            if path.is_file()
        ),
        key=lambda path: path.relative_to(CONTRACTS_ROOT).as_posix().encode("utf-8"),
    )
    return [
        {
            "path": path.relative_to(CONTRACTS_ROOT).as_posix(),
            "sha256": sha256(path),
            "size": path.stat().st_size,
        }
        for path in files
    ]


def aggregate_digest(entries: list[dict[str, Any]]) -> str:
    material = b"".join(
        entry["path"].encode("utf-8")
        + b"\0"
        + entry["sha256"].encode("ascii")
        + b"\n"
        for entry in entries
    )
    return hashlib.sha256(material).hexdigest()


def _load_source_manifest(source_root: Path) -> dict[str, Any]:
    source_manifest_path = source_root / SOURCE_MANIFEST
    if not source_manifest_path.is_file():
        raise SystemExit(f"approved source checkout is missing {SOURCE_MANIFEST}")
    if sha256(source_manifest_path) != APPROVED_SOURCE_MANIFEST_SHA256:
        raise SystemExit(
            f"source manifest does not match {APPROVED_SOURCE_COMMIT}: {SOURCE_MANIFEST}"
        )
    value = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if set(value) != {"files", "manifest_version", "profiles"} or value["manifest_version"] != 1:
        raise SystemExit("approved source contract manifest has an invalid envelope")
    if len(value["files"]) != EXPECTED_PUBLISHED_FILE_COUNT:
        raise SystemExit("approved source contract manifest has an unexpected file count")
    if tuple(value["profiles"]) != EXPECTED_PUBLISHED_PROFILES:
        raise SystemExit("approved source contract manifest profiles drifted")
    for entry in value["files"]:
        if set(entry) != {"path", "sha256", "size"}:
            raise SystemExit("approved source contract manifest contains an invalid file record")
        source = source_root / entry["path"]
        if (
            not source.is_file()
            or source.stat().st_size != entry["size"]
            or sha256(source) != entry["sha256"]
        ):
            raise SystemExit(
                f"approved source file does not match {APPROVED_SOURCE_COMMIT}: {entry['path']}"
            )
    return value


def _copy_exact_file(source: Path, target: Path, source_label: str) -> None:
    if target.is_file():
        if target.read_bytes() != source.read_bytes():
            previous = APPROVED_PREVIOUS_INVENTORIES.get(source_label)
            if previous is None or sha256(target) != previous:
                raise SystemExit(f"existing vendored upstream file is immutable: {source_label}")
            shutil.copyfile(source, target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def copy_published_contracts(source_root: Path) -> None:
    source_manifest = _load_source_manifest(source_root)
    expected_by_root: dict[str, set[Path]] = {root: set() for root in PUBLISHED_ROOTS}
    for entry in source_manifest["files"]:
        relative = Path(entry["path"])
        root = relative.parts[0]
        if root not in expected_by_root:
            raise SystemExit(f"published source manifest names an unexpected root: {root}")
        expected_by_root[root].add(relative)
        _copy_exact_file(source_root / relative, CONTRACTS_ROOT / relative, entry["path"])

    for root, expected in expected_by_root.items():
        target_root = CONTRACTS_ROOT / root
        actual = {
            path.relative_to(CONTRACTS_ROOT)
            for path in target_root.rglob("*")
            if path.is_file()
        }
        if actual != expected:
            difference = sorted(
                actual ^ expected, key=lambda path: path.as_posix().encode("utf-8")
            )
            raise SystemExit(
                f"published {root} export differs from pinned source manifest: "
                + ", ".join(path.as_posix() for path in difference)
            )

    source_manifest_target = CONTRACTS_ROOT / SOURCE_MANIFEST_TARGET
    _copy_exact_file(source_root / SOURCE_MANIFEST, source_manifest_target, SOURCE_MANIFEST)


def copy_immutable_export(source: Path, target: Path, source_path: str) -> None:
    if not target.exists():
        shutil.copytree(source, target)
        return
    source_files = {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file()
    }
    target_files = {
        path.relative_to(target).as_posix(): path
        for path in target.rglob("*")
        if path.is_file()
    }
    if source_files.keys() != target_files.keys():
        difference = sorted(source_files.keys() ^ target_files.keys())
        raise SystemExit(
            f"existing vendored export differs from approved source {source_path}: "
            + ", ".join(difference)
        )
    for relative, source_file in source_files.items():
        if source_file.read_bytes() != target_files[relative].read_bytes():
            raise SystemExit(f"existing vendored upstream fixture is immutable: {target / relative}")


def verify_approved_source_exports(source_root: Path) -> None:
    for export, source_path in EXPORT_SOURCES.items():
        source = source_root / source_path
        if not source.is_dir():
            raise SystemExit(f"approved source checkout does not contain export source: {source_path}")
        actual = _tree_digest(source)
        expected = APPROVED_EXPORT_DIGESTS[export]
        if actual != expected:
            raise SystemExit(
                f"approved source export does not match {APPROVED_SOURCE_COMMIT}: {source_path}"
            )


def selected_progress_profile() -> str:
    progress_root = CONTRACTS_ROOT / "onboarding-progress"
    actual = {
        path.relative_to(progress_root).as_posix()
        for path in progress_root.rglob("*")
        if path.is_file()
    }
    if actual != EXPECTED_PROGRESS_VECTOR_FILES:
        difference = sorted(actual ^ EXPECTED_PROGRESS_VECTOR_FILES)
        raise SystemExit("selected onboarding-progress export drifted: " + ", ".join(difference))
    return EXPECTED_PROGRESS_PROFILE


def selected_pairing_profile() -> str:
    profile_root = CONTRACTS_ROOT / "companion-pairing-profile"
    actual_profile = {
        path.relative_to(profile_root).as_posix()
        for path in profile_root.rglob("*")
        if path.is_file()
    }
    if actual_profile != EXPECTED_PAIRING_PROFILE_FILES:
        difference = sorted(actual_profile ^ EXPECTED_PAIRING_PROFILE_FILES)
        raise SystemExit("selected companion-pairing profile export drifted: " + ", ".join(difference))
    pairing_root = CONTRACTS_ROOT / "companion-pairing-v1"
    actual_vectors = {
        path.relative_to(pairing_root).as_posix()
        for path in pairing_root.rglob("*")
        if path.is_file()
    }
    if actual_vectors != EXPECTED_PAIRING_VECTOR_FILES:
        difference = sorted(actual_vectors ^ EXPECTED_PAIRING_VECTOR_FILES)
        raise SystemExit("selected companion-pairing vector export drifted: " + ", ".join(difference))
    record = json.loads((CONTRACTS_ROOT / PAIRING_PROFILE_RECORD).read_text("utf-8"))
    vectors = json.loads(
        (CONTRACTS_ROOT / VECTOR_MANIFESTS["companion-pairing-v1"]).read_text("utf-8")
    )
    if record["profile"] != EXPECTED_PAIRING_PROFILE or vectors["profile"] != record["profile"]:
        raise SystemExit("selected companion-pairing record and vectors name different profiles")
    return EXPECTED_PAIRING_PROFILE


def _published_profiles() -> list[str]:
    """Inventory exported profiles; this is not runtime support advertising."""

    source_manifest_path = CONTRACTS_ROOT / SOURCE_MANIFEST_TARGET
    value = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if sha256(source_manifest_path) != APPROVED_SOURCE_MANIFEST_SHA256:
        raise SystemExit("vendored source contract manifest does not match approved authority")
    profiles = list(value["profiles"])
    if tuple(profiles) != EXPECTED_PUBLISHED_PROFILES:
        raise SystemExit("vendored published profile inventory drifted")
    profiles.extend(LEGACY_VECTOR_PROFILE_ALIASES)
    profiles.extend(
        json.loads((CONTRACTS_ROOT / path).read_text("utf-8"))["profile"]
        for path in POLICY_PROFILES.values()
    )
    return profiles


def build_records() -> tuple[dict[str, Any], dict[str, Any]]:
    vector_manifests = [
        {"export": export, "path": path, "sha256": sha256(CONTRACTS_ROOT / path)}
        for export, path in VECTOR_MANIFESTS.items()
    ]
    generator_versions = [
        {
            "export": entry["export"],
            "version": json.loads((CONTRACTS_ROOT / entry["path"]).read_text("utf-8"))[
                "generator_version"
            ],
        }
        for entry in vector_manifests
    ]
    trust_sets = [
        {"export": export, "path": path, "sha256": sha256(CONTRACTS_ROOT / path)}
        for export, path in TRUST_SETS.items()
    ]
    conformance_manifests = [
        {"export": export, "path": path, "sha256": sha256(CONTRACTS_ROOT / path)}
        for export, path in CONFORMANCE_MANIFESTS.items()
    ]
    profiles = _published_profiles()
    if selected_progress_profile() not in profiles:
        raise SystemExit("selected onboarding-progress profile is absent from published profiles")
    if selected_pairing_profile() not in profiles:
        raise SystemExit("selected companion-pairing profile is absent from published profiles")
    entries = fixture_entries()
    manifest = {
        "content_digest": aggregate_digest(entries),
        "export_set": EXPORT_SET,
        "files": entries,
        "manifest_version": 2,
        "profiles": profiles,
    }
    manifest_bytes = encode(manifest)
    pin = {
        "aggregate_bundle_sha256": manifest["content_digest"],
        "consumer_pin_version": 3,
        "contract_manifest_path": "manifest.json",
        "contract_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "export_set": EXPORT_SET,
        "generator_versions": generator_versions,
        "supported_profiles": profiles,
        "trust_sets": trust_sets,
        "vector_manifests": vector_manifests,
        "conformance_manifests": conformance_manifests,
        "source_repository": APPROVED_SOURCE_REPOSITORY,
        "source_commit": APPROVED_SOURCE_COMMIT,
        "source_tree": APPROVED_SOURCE_TREE,
        "source_contract_manifest_path": SOURCE_MANIFEST_TARGET,
        "source_contract_manifest_sha256": APPROVED_SOURCE_MANIFEST_SHA256,
    }
    return manifest, pin


def copy_selected_export(source_root: Path) -> None:
    _load_source_manifest(source_root)
    verify_approved_source_exports(source_root)
    for export, source_path in EXPORT_SOURCES.items():
        source = source_root / source_path
        target = CONTRACTS_ROOT / export
        copy_immutable_export(source, target, source_path)
    copy_published_contracts(source_root)

    count = len(fixture_entries())
    if count != EXPECTED_FILE_COUNT:
        raise SystemExit(f"selected export has {count} files; expected {EXPECTED_FILE_COUNT}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the selected contract snapshot and public pin."
    )
    parser.add_argument(
        "--copy-from",
        type=Path,
        help="clean, approved source checkout; only its selected export is copied",
    )
    parser.add_argument("--check", action="store_true", help="fail if generated records are stale")
    args = parser.parse_args()
    if args.copy_from is not None:
        if args.check:
            raise SystemExit("--copy-from and --check cannot be combined")
        copy_selected_export(args.copy_from.resolve())
    manifest, pin = build_records()
    expected = {
        CONTRACTS_ROOT / "manifest.json": encode(manifest),
        CONTRACTS_ROOT / "consumer-pin.json": encode(pin),
    }
    stale = [
        path.name
        for path, content in expected.items()
        if not path.is_file() or path.read_bytes() != content
    ]
    if args.check:
        if stale:
            raise SystemExit(f"selected contract snapshot is stale: {', '.join(stale)}")
        print(f"selected export records are current ({len(manifest['files'])} files)")
        return 0
    for path, content in expected.items():
        path.write_bytes(content)
    print(f"regenerated selected export records ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
