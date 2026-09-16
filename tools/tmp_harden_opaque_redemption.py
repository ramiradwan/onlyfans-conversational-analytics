from __future__ import annotations

from pathlib import Path


loader = Path("contracts/loader.py")
source = loader.read_text(encoding="utf-8")
replacements = {
    '_APPROVED_SOURCE_COMMIT = "50c08ee8b3f3dbb1364b875e876a32ab7c641f9a"': (
        '_APPROVED_SOURCE_COMMIT = "de2e514e4ef59f5789d2806002c5eb439709d261"'
    ),
    '_APPROVED_SOURCE_TREE = "15b821c361f4bc1077a0e1ef5689f5916ff75f51"': (
        '_APPROVED_SOURCE_TREE = "ffc0702716cef54e02fbd888cd259087e6623a20"'
    ),
    '_APPROVED_SOURCE_MANIFEST_SHA256 = "d50e961dd421bdb8be4fd8860653c5bd1a8f7759b2fd60ed263b3245aff0fd07"': (
        '_APPROVED_SOURCE_MANIFEST_SHA256 = "6a4a2e2c1050b7e729695dbc1877bb8f60abfea460c37d74e5c275d3e9c86963"'
    ),
    "    manifest = verify_snapshot_integrity()\n    candidate = (manifest_root := _contracts_root()) / Path(path)": (
        "    verify_snapshot_integrity()\n    candidate = (manifest_root := _contracts_root()) / Path(path)"
    ),
}
for old, new in replacements.items():
    if old not in source and new not in source:
        raise SystemExit(f"missing loader pin: {old}")
    source = source.replace(old, new, 1)
loader.write_text(source, encoding="utf-8")

path = Path("app/security/capability_license_redemption.py")
source = path.read_text(encoding="utf-8")
old_request = '''        response = self._request(
            _REDEMPTION_PATH,
            body,
            headers={"Idempotency-Key": str(uuid4())},
        )
        if _retryable(response.status_code):'''
new_request = '''        idempotency_headers = {"Idempotency-Key": str(uuid4())}
        response: TransportResponse | None = None
        for attempt in range(2):
            try:
                response = self._request(
                    _REDEMPTION_PATH,
                    body,
                    headers=idempotency_headers,
                )
                break
            except CapabilityLicenseRedemptionUnavailable:
                if attempt == 1:
                    raise
        assert response is not None
        if _retryable(response.status_code):'''
if old_request not in source and new_request not in source:
    raise SystemExit("redemption request block not found")
source = source.replace(old_request, new_request, 1)
old_catch = '''            except (CapabilityLicenseRedemptionUnavailable, InstallationKeyError):
                return "hosted_unavailable"'''
new_catch = '''            except InstallationKeyError:
                return "installation_key_unavailable"
            except CapabilityLicenseRedemptionUnavailable:
                return "hosted_unavailable"'''
if old_catch not in source and new_catch not in source:
    raise SystemExit("redemption exception block not found")
source = source.replace(old_catch, new_catch, 1)
path.write_text(source, encoding="utf-8")

tests = Path("tests/test_capability_license_opaque_redemption.py")
source = tests.read_text(encoding="utf-8")
anchor = "def test_ambiguous_commit_reproofs_same_continuation_and_recovers_completed() -> None:\n"
exact_retry = '''def test_exact_transport_retry_reuses_idempotency_key_before_reproof() -> None:
    key = _key()
    proof = ProofAuthority(key)
    transport = QueueTransport(
        [_challenge(), OSError("response lost after hosted commit"), _delivery()]
    )
    result = CapabilityLicenseRedemptionClient(transport, proof).redeem(
        CONTINUATION,
        organization_id=ORGANIZATION_ID,
        installation_id=INSTALLATION_ID,
        key=key,
    )
    assert result.hosted_result == "accepted"
    assert len(proof.signed) == 1
    assert len(transport.requests) == 3
    assert transport.headers[1] == transport.headers[2]
    assert transport.headers[1] is not None


'''
if exact_retry.strip() not in source:
    if anchor not in source:
        raise SystemExit("ambiguous retry test anchor missing")
    source = source.replace(anchor, exact_retry + anchor, 1)
old_queue = '''            OSError("response lost after hosted commit"),
            _challenge(),'''
new_queue = '''            OSError("response lost after hosted commit"),
            OSError("exact retry result also lost"),
            _challenge(),'''
if old_queue in source:
    source = source.replace(old_queue, new_queue, 1)
old_count = '    assert len(redemption_requests) == 2\n'
new_count = (
    '    assert len(redemption_requests) == 3\n'
    '    assert transport.headers[1] == transport.headers[2]\n'
    '    assert transport.headers[4] != transport.headers[1]\n'
)
if old_count in source:
    source = source.replace(old_count, new_count, 1)
route_test = '''

def test_normal_runtime_registers_opaque_redemption_route() -> None:
    from app.main import app

    assert str(app.url_path_for("redeem_capability_license_continuation")) == (
        "/api/v1/capability-license/redeem"
    )
'''
if "test_normal_runtime_registers_opaque_redemption_route" not in source:
    source += route_test
tests.write_text(source, encoding="utf-8")

snapshot_tests = Path("tests/test_contract_snapshot.py")
source = snapshot_tests.read_text(encoding="utf-8")
pin_replacements = {
    'assert pin["aggregate_bundle_sha256"] == "3c4b0a774e2fe6f3bf878c6e8434bc0e2c712630e7c89951933788d70cf1ed6c"': (
        'assert pin["aggregate_bundle_sha256"] == "5ced81122666923d82eb212ffd0d4643e56952fe6027a1308c12cc06c50315a1"'
    ),
    'assert pin["contract_manifest_sha256"] == "6ba604cfa5b85cd75354fe6dd0ab55c89a0adfa005d6d161c659485cbd1d0878"': (
        'assert pin["contract_manifest_sha256"] == "827eda31ec62e156fcc16cf7b7dbf09ddddb1019ed920e322be28d21c5bf30f4"'
    ),
}
for old, new in pin_replacements.items():
    if old not in source and new not in source:
        raise SystemExit(f"missing snapshot pin assertion: {old}")
    source = source.replace(old, new, 1)
snapshot_tests.write_text(source, encoding="utf-8")
