from pathlib import Path
import hashlib
import json
import subprocess

root = Path("contracts")
catalog = root / "catalog/contracts.yaml"
source = catalog.read_text(encoding="utf-8")
addition = """  - id: capability-license-redemption-continuation-v1
    owner: commercial-ledger
    producer: commercial-control-plane
    consumer: local-product
    purpose: Transfer only an opaque short-lived customer-authorized commercial redemption reference without exposing delivery coordinates.
    profile: urn:bridge-clean:capability-license-redemption-continuation:v1
    classification: cross-plane-security
    retention_class: security-audit
    max_request_bytes: 0
    max_response_bytes: 1024
    schema_paths:
      - schemas/commercial/v1/capability-license-redemption-continuation.schema.json
    openapi_operations: []
    governing_adr: docs/adr/0021-add-opaque-capability-license-redemption.md
  - id: capability-license-redemption-proof-v1
    owner: commercial-ledger
    producer: local-product
    consumer: commercial-control-plane
    purpose: Issue a redemption-specific challenge only after opaque continuation coordinates match current local identity authority.
    profile: urn:bridge-clean:capability-license-redemption-proof:v1
    classification: cross-plane-security
    retention_class: security-audit
    max_request_bytes: 4096
    max_response_bytes: 4096
    schema_paths:
      - schemas/commercial/v1/capability-license-redemption-proof-challenge-request.schema.json
      - schemas/commercial/v1/capability-license-redemption-proof-challenge-response.schema.json
    openapi_operations:
      - issueCapabilityLicenseRedemptionProofChallenge
    governing_adr: docs/adr/0021-add-opaque-capability-license-redemption.md
  - id: capability-license-redemption-v1
    owner: commercial-ledger
    producer: local-product
    consumer: commercial-control-plane
    purpose: Redeem one server-bound opaque commercial authorization using exact local installation-key proof and return protected ADR 0020 delivery inputs to Brain only.
    profile: urn:bridge-clean:capability-license-redemption:v1
    classification: commercial-confidential
    retention_class: payment-tax-legal-review-required
    max_request_bytes: 8192
    max_response_bytes: 8192
    schema_paths:
      - schemas/commercial/v1/capability-license-redemption-request.schema.json
      - schemas/commercial/v1/capability-license-redemption-response.schema.json
    openapi_operations:
      - redeemCapabilityLicenseContinuation
    governing_adr: docs/adr/0021-add-opaque-capability-license-redemption.md
"""
if "capability-license-redemption-continuation-v1" not in source:
    if not source.endswith("\n"):
        source += "\n"
    catalog.write_text(source + addition, encoding="utf-8")

profiles = [
    "urn:bridge-clean:bootstrap-recovery:v1",
    "urn:bridge-clean:bootstrap-recovery:v2",
    "urn:bridge-clean:capability-license-activation-package:v1",
    "urn:bridge-clean:capability-license-activation-proof:v1",
    "urn:bridge-clean:capability-license-activation:v1",
    "urn:bridge-clean:capability-license-exchange-quote:v1",
    "urn:bridge-clean:capability-license-exchange:v1",
    "urn:bridge-clean:capability-license-redemption-continuation:v1",
    "urn:bridge-clean:capability-license-redemption-proof:v1",
    "urn:bridge-clean:capability-license-redemption:v1",
    "urn:bridge-clean:capability-license-reissue-authorization:v1",
    "urn:bridge-clean:capability-license-reissue-package:v1",
    "urn:bridge-clean:capability-license-reissue:v1",
    "urn:bridge-clean:capability-license:v1",
    "urn:bridge-clean:capability-permit:v1",
    "urn:bridge-clean:companion-pairing:v1",
    "urn:bridge-clean:creator-association:v1",
    "urn:bridge-clean:grant-profile:v1",
    "urn:bridge-clean:installation-claim-package:v1",
    "urn:bridge-clean:installation-claim-package:v2",
    "urn:bridge-clean:installation-claim:v1",
    "urn:bridge-clean:installation-claim:v2",
    "urn:bridge-clean:onboarding-progress:v1",
    "urn:bridge-clean:provisioning-proof:v1",
    "urn:bridge-clean:release-descriptor:v1",
]
normative = []
for directory in ("catalog", "schemas", "openapi", "profiles"):
    normative.extend(path for path in (root / directory).rglob("*") if path.is_file())
normative.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
manifest = {
    "manifest_version": 1,
    "profiles": profiles,
    "files": [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": len(path.read_bytes()),
        }
        for path in normative
    ],
}
encoded = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
digest = hashlib.sha256(encoded).hexdigest()
expected = "6a4a2e2c1050b7e729695dbc1877bb8f60abfea460c37d74e5c275d3e9c86963"
if digest != expected:
    raise SystemExit(f"qualified contract manifest mismatch: {digest}")
source_manifest = root / "source-contract-manifest/contract-manifest.json"
source_manifest.parent.mkdir(parents=True, exist_ok=True)
source_manifest.write_bytes(encoded)

script = Path("tools/regenerate_contract_snapshot.py")
text = script.read_text(encoding="utf-8")
replacements = {
    'APPROVED_SOURCE_COMMIT = "50c08ee8b3f3dbb1364b875e876a32ab7c641f9a"':
        'APPROVED_SOURCE_COMMIT = "de2e514e4ef59f5789d2806002c5eb439709d261"',
    'APPROVED_SOURCE_TREE = "15b821c361f4bc1077a0e1ef5689f5916ff75f51"':
        'APPROVED_SOURCE_TREE = "ffc0702716cef54e02fbd888cd259087e6623a20"',
    'APPROVED_SOURCE_MANIFEST_SHA256 = "d50e961dd421bdb8be4fd8860653c5bd1a8f7759b2fd60ed263b3245aff0fd07"':
        'APPROVED_SOURCE_MANIFEST_SHA256 = "6a4a2e2c1050b7e729695dbc1877bb8f60abfea460c37d74e5c275d3e9c86963"',
    "EXPECTED_FILE_COUNT = 774": "EXPECTED_FILE_COUNT = 783",
    "EXPECTED_PUBLISHED_FILE_COUNT = 55": "EXPECTED_PUBLISHED_FILE_COUNT = 64",
}
for old, new in replacements.items():
    if old not in text and new not in text:
        raise SystemExit(f"missing expected snapshot pin: {old}")
    text = text.replace(old, new, 1)
anchor = '    "urn:bridge-clean:capability-license-exchange:v1",\n'
profiles_block = (
    '    "urn:bridge-clean:capability-license-redemption-continuation:v1",\n'
    '    "urn:bridge-clean:capability-license-redemption-proof:v1",\n'
    '    "urn:bridge-clean:capability-license-redemption:v1",\n'
)
if profiles_block not in text:
    if anchor not in text:
        raise SystemExit("published profile anchor missing")
    text = text.replace(anchor, anchor + profiles_block, 1)
script.write_text(text, encoding="utf-8")

subprocess.run(["python", str(script)], check=True)
subprocess.run(["python", str(script), "--check"], check=True)
